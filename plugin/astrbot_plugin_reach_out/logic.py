# -*- coding: utf-8 -*-
"""主动联系的纯逻辑：授权白名单、限流、去重、近况（跨会话经历）与隐私过滤，以及 JSON 持久化。

不依赖 AstrBot，方便单测。所有会改状态的方法都在 asyncio.Lock 里做完“检查 + 修改 + 落盘”，
发消息本身不在锁里：先 reserve（占额度），发失败再 refund（退额度）。
"""
import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

USER = "user"
GROUP = "group"

_KIND_ALIASES = {
    USER: USER, "private": USER, "friend": USER, "私聊": USER, "私信": USER, "好友": USER, "人": USER,
    GROUP: GROUP, "群": GROUP, "群聊": GROUP, "qq群": GROUP,
}
_ID_RE = re.compile(r"^\d{5,12}$")

DAY = 86400
HOUR = 3600
SHANGHAI = timezone(timedelta(hours=8))  # 宿主机是 UTC+11，时间一律按北京时间显示
_TAG_RE = re.compile(r"^\s*\[[^\[\]\r\n]{1,40}\]\s*")


def hm(ts):
    return datetime.fromtimestamp(float(ts), SHANGHAI).strftime("%H:%M")


def md_hm(ts):
    return datetime.fromtimestamp(float(ts), SHANGHAI).strftime("%m-%d %H:%M")


def strip_tag(text):
    """去掉 face_convert 加在开头的身份标记，如 "[搭档(123)] "。"""
    return _TAG_RE.sub("", str(text or ""), count=1)


def clip(text, n):
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def normalize_kind(kind):
    return _KIND_ALIASES.get(str(kind or "").strip().lower())


def normalize_id(raw):
    s = str(raw or "").strip()
    return s if _ID_RE.match(s) else None


def kind_label(kind):
    return "私聊" if kind == USER else "群"


def target_key(kind, tid):
    return f"{kind}:{tid}"


def build_umo(platform_id, kind, tid):
    mtype = "FriendMessage" if kind == USER else "GroupMessage"
    return f"{platform_id}:{mtype}:{tid}"


def clean_text(text, max_len):
    """返回 (文本, 错误)。只去首尾空白，不改内容。"""
    if not isinstance(text, str):
        return None, "消息内容不是文字"
    s = text.strip()
    if not s:
        return None, "消息内容是空的"
    if len(s) > max_len:
        return None, f"消息太长了（{len(s)} 字，上限 {max_len} 字），精简后再发"
    return s, None


def _hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Settings:
    platform_id: str = ""
    max_text_len: int = 300
    partner_per_hour: int = 6
    partner_per_day: int = 20
    contact_per_hour: int = 10
    contact_per_target_day: int = 10
    dedupe_minutes: int = 10
    default_grant_days: int = 7
    thought_delay_seconds: int = 2
    thought_sender_id: str = "0"
    recent_hours: int = 6
    recent_max: int = 30
    recent_inject_max: int = 12

    @classmethod
    def from_config(cls, cfg):
        cfg = cfg or {}
        s = cls()
        for name, default in cls().__dict__.items():
            if name not in cfg:
                continue
            v = cfg.get(name)
            try:
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v)
                    if v < 0:
                        continue
                else:
                    v = str(v or "").strip()
            except (TypeError, ValueError):
                continue
            setattr(s, name, v)
        if s.max_text_len < 1:
            s.max_text_len = 1
        if not normalize_id(s.thought_sender_id) and s.thought_sender_id != "0":
            s.thought_sender_id = "0"
        return s


def parse_grant_args(tokens, default_days):
    """解析 `/授权 私聊|群 <号码> [备注] [天数] [条数]`。

    `7天` / `5条` 带单位时按单位认；裸数字依次当天数、条数；其余拼成备注。
    天数 0 = 永久，条数 0 = 不限。返回 (dict, 错误)。
    """
    tokens = [t for t in (tokens or []) if str(t).strip()]
    if len(tokens) < 2:
        return None, "用法：/授权 私聊|群 <号码> [备注] [天数] [条数]（天数 0=永久，条数 0=不限）"
    kind = normalize_kind(tokens[0])
    if not kind:
        return None, f"类型「{tokens[0]}」不认识，用 私聊 或 群"
    tid = normalize_id(tokens[1])
    if not tid:
        return None, f"号码「{tokens[1]}」不对，应为 5～12 位数字"
    days = count = None
    plain_nums = []
    note_parts = []
    for t in tokens[2:]:
        t = str(t).strip()
        m = re.fullmatch(r"(\d+)\s*天", t)
        if m:
            days = int(m.group(1))
            continue
        m = re.fullmatch(r"(\d+)\s*条", t)
        if m:
            count = int(m.group(1))
            continue
        if t.isdigit():
            plain_nums.append(int(t))
            continue
        note_parts.append(t)
    for n in plain_nums:
        if days is None:
            days = n
        elif count is None:
            count = n
        else:
            return None, "数字太多了：最多一个天数、一个条数"
    if days is None:
        days = default_days
    if days > 3650 or (count or 0) > 100000:
        return None, "天数或条数太大了"
    return {"kind": kind, "id": tid, "note": " ".join(note_parts)[:40],
            "days": days, "count": count or 0}, None


class Store:
    """状态文件：grants（白名单）、sent（近 2 天的发送记录，用于限流和去重）、recent（近况）。"""

    def __init__(self, path, settings, now=time.time, logger=None):
        self.path = path
        self.s = settings
        self.now = now
        self.log = logger
        self._lock = asyncio.Lock()
        self.state = self._load()

    # ---------- 持久化 ----------
    def _empty(self):
        return {"version": 2, "grants": {}, "sent": [], "recent": []}

    def _load(self):
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("顶层不是对象")
            st = self._empty()
            if isinstance(data.get("grants"), dict):
                st["grants"] = data["grants"]
            for k in ("sent", "recent"):
                if isinstance(data.get(k), list):
                    st[k] = [e for e in data[k] if isinstance(e, dict)]
            return st
        except Exception as e:
            bad = f"{self.path}.corrupt-{int(self.now())}"
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            if self.log:
                self.log.warning(f"[reach_out] 状态文件损坏（{type(e).__name__}），已移到 {bad}，从空白开始")
            return self._empty()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---------- 清理 ----------
    def _prune(self):
        now = self.now()
        self.state["sent"] = [e for e in self.state["sent"] if now - float(e.get("ts", 0)) < 2 * DAY]
        for k, g in list(self.state["grants"].items()):
            # 正在发送中的（inflight>0）先留着，发失败时要退条数
            if not self._grant_alive(g, now) and not int(g.get("inflight") or 0):
                del self.state["grants"][k]
        ttl = self.s.recent_hours * HOUR
        self.state["recent"] = [e for e in self.state["recent"]
                                if now - float(e.get("ts", 0)) < ttl][-self.s.recent_max:]

    @staticmethod
    def _grant_alive(g, now):
        exp = g.get("expires_at")
        if exp is not None and now >= float(exp):
            return False
        rem = g.get("remaining")
        if rem is not None and int(rem) <= 0:
            return False
        return True

    # ---------- 授权 ----------
    async def grant(self, kind, tid, note="", days=7, count=0, by=""):
        async with self._lock:
            now = self.now()
            g = {"kind": kind, "id": tid, "note": note or "",
                 "expires_at": (now + days * DAY) if days else None,
                 "remaining": count if count else None,
                 "granted_by": str(by), "granted_at": now}
            self.state["grants"][target_key(kind, tid)] = g
            self._prune()
            self._save()
            return dict(g)

    async def revoke(self, kind, tid):
        async with self._lock:
            existed = self.state["grants"].pop(target_key(kind, tid), None) is not None
            self._prune()
            self._save()
            return existed

    async def list_grants(self):
        async with self._lock:
            self._prune()
            now = self.now()
            return [dict(g) for g in self.state["grants"].values() if self._grant_alive(g, now)]

    # ---------- 限流 ----------
    def _count(self, pred, window):
        now = self.now()
        return sum(1 for e in self.state["sent"] if now - float(e.get("ts", 0)) < window and pred(e))

    def _dup(self, key, h):
        return self._count(lambda e: e.get("key") == key and e.get("h") == h, self.s.dedupe_minutes * 60) > 0

    async def reserve_partner(self, partner_ids, text):
        """给搭档发：检查限流和去重，通过则记一条发送记录。返回 (token, 错误)。"""
        async with self._lock:
            self._prune()
            h = _hash(text)
            ch = lambda e: e.get("ch") == "partner"
            if self._count(ch, HOUR) >= self.s.partner_per_hour:
                return None, f"这一小时已经私信搭档 {self.s.partner_per_hour} 次了，攒着下次一起说"
            if self._count(ch, DAY) >= self.s.partner_per_day:
                return None, f"今天已经私信搭档 {self.s.partner_per_day} 次了，明天再说"
            key = "partner:" + ",".join(sorted(partner_ids))
            if self._dup(key, h):
                return None, "同样的话刚刚已经发给搭档了，不用重复发"
            entry = {"ts": self.now(), "ch": "partner", "key": key, "h": h}
            self.state["sent"].append(entry)
            self._save()
            return {"entry": entry, "grant_key": None}, None

    async def reserve_contact(self, kind, tid, text):
        """给授权目标发：授权、条数、限流、去重都过了才扣一条。返回 (token, 错误)。"""
        async with self._lock:
            self._prune()
            key = target_key(kind, tid)
            g = self.state["grants"].get(key)
            if not g or not self._grant_alive(g, self.now()):
                return None, f"{kind_label(kind)} {tid} 没有授权（或已过期、条数用完），需要搭档先授权"
            h = _hash(text)
            ch = lambda e: e.get("ch") == "contact"
            if self._count(ch, HOUR) >= self.s.contact_per_hour:
                return None, f"这一小时已经主动发了 {self.s.contact_per_hour} 条，歇一会儿再发"
            if self._count(lambda e: e.get("key") == key, DAY) >= self.s.contact_per_target_day:
                return None, f"今天已经给{kind_label(kind)} {tid} 发了 {self.s.contact_per_target_day} 条，明天再说"
            if self._dup(key, h):
                return None, "同样的话刚刚已经发过了，不用重复发"
            entry = {"ts": self.now(), "ch": "contact", "key": key, "h": h}
            self.state["sent"].append(entry)
            if g.get("remaining") is not None:
                g["remaining"] = int(g["remaining"]) - 1
            g["inflight"] = int(g.get("inflight") or 0) + 1
            self._save()
            return {"entry": entry, "grant_key": key, "grant_snapshot": dict(g)}, None

    async def commit(self, token):
        """发送成功：结束占用。"""
        if not token or not token.get("grant_key"):
            return
        async with self._lock:
            g = self.state["grants"].get(token["grant_key"])
            if g is not None:
                g["inflight"] = max(0, int(g.get("inflight") or 0) - 1)
            self._prune()
            self._save()

    async def refund(self, token):
        """发送失败：删掉那条发送记录，退回授权条数。授权已被撤销的不复活。"""
        if not token:
            return
        async with self._lock:
            entry = token.get("entry")
            self.state["sent"] = [e for e in self.state["sent"] if e != entry]
            gk = token.get("grant_key")
            g = self.state["grants"].get(gk) if gk else None
            if g is not None:
                g["inflight"] = max(0, int(g.get("inflight") or 0) - 1)
                if g.get("remaining") is not None:
                    g["remaining"] = int(g["remaining"]) + 1
            self._prune()
            self._save()

    # ---------- 近况 ----------
    async def add_recent(self, key, place, who, text, merge_seconds=0):
        """记一条近况。merge_seconds>0 时，同一会话同一说话人在这段时间内的连续几条合并成一条
        （她的回复会分成几个气泡分别发出）。"""
        text = str(text or "").strip()
        if not text:
            return
        async with self._lock:
            now = self.now()
            rec = self.state["recent"]
            last = rec[-1] if rec else None
            if (merge_seconds and last and last.get("key") == key and last.get("who") == who
                    and now - float(last.get("ts", 0)) <= merge_seconds):
                last["text"] = clip(f"{last.get('text', '')} {text}", 120)
                last["ts"] = now
            else:
                rec.append({"ts": now, "key": key, "place": place, "who": who, "text": clip(text, 120)})
            self._prune()
            self._save()

    async def recent(self):
        async with self._lock:
            self._prune()
            return [dict(e) for e in self.state["recent"]]


def render_recent(entries, current_key, viewer, partners, max_lines=12, text_len=60):
    """把近况按隐私规则排成几行。

    viewer: "partner"（和搭档私聊）/ "user"（和其他人私聊）/ "group"（群聊）。
    - 当前会话自己的条目不注入（那边的聊天记录里本来就有）。
    - 搭档私聊看全部。
    - 其他人私聊：只看群里的事；任何私聊都不提。
    - 群聊：群里的事看全文；私聊只说“和谁私聊过”，不带内容。
    """
    partners = set(partners or [])
    lines = []
    collapsed = {}
    for e in entries:
        key = str(e.get("key", ""))
        if not key or key == current_key:
            continue
        private = key.startswith(USER + ":")
        if private and viewer == "user":
            continue
        if private and viewer == "group":
            pid = key.split(":", 1)[1]
            label = "和搭档" if pid in partners else "和别人"
            collapsed[key] = (float(e.get("ts", 0)), label)
            continue
        lines.append((float(e.get("ts", 0)),
                      f"- {hm(e.get('ts', 0))} {e.get('place', '')} {e.get('who', '')}：{clip(e.get('text', ''), text_len)}"))
    for key, (ts, label) in collapsed.items():
        lines.append((ts, f"- {hm(ts)} {label}私聊过（内容不在这里说）"))
    lines.sort(key=lambda x: x[0])
    return [l for _, l in lines][-max_lines:]
