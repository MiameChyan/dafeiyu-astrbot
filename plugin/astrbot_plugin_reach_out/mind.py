# -*- coding: utf-8 -*-
"""心事账本：她挂在心上、还没了结的事（约定 / 牵挂 / 想分享）。

纯逻辑，不依赖 AstrBot。和 logic.Store 一样：改状态的方法都在 asyncio.Lock 里做完
“检查 + 修改 + 落盘”，状态文件原子写入，损坏时移到 .corrupt-<时间> 后从空开始。

可见范围由 origin（在哪个会话里记下的）决定，规则和近况一致：
- 搭档私聊：全部可见；
- 和 X 私聊：在和 X 的私聊里记下的，加上在群里记下的、关于 X 的；
- 群：在群里记下的；私聊里记下的永远不在群里出现。
"""
import asyncio
import difflib
import json
import os
import re
import time
import uuid

try:
    from . import logic as L
except ImportError:  # 测试里按文件加载
    import logic as L

KINDS = {
    "约定": "约定", "promise": "约定", "答应": "约定",
    "牵挂": "牵挂", "care": "牵挂", "惦记": "牵挂",
    "想分享": "想分享", "share": "想分享", "分享": "想分享",
}
ACTIVE = "active"
FINISHED = ("done", "dropped", "expired")
HOUR = 3600


def normalize_kind(kind):
    return KINDS.get(str(kind or "").strip().lower())


def parse_hours(raw, max_hours):
    """返回 (小时数或 None, 错误)。空 = 没有到期时间。"""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None, None
    try:
        h = float(s)
    except ValueError:
        return None, "due_in_hours 要是数字（几小时后），不知道就留空"
    if h != h or h < 0:  # NaN 或负数
        return None, "due_in_hours 不能是负数"
    return min(h, float(max_hours)), None


def similar(a, b):
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def visible(item, current_key, viewer, partners):
    """viewer: partner / user / group（同近况）。"""
    origin = str(item.get("origin", ""))
    if viewer == "partner":
        return True
    if viewer == "user":
        if origin == current_key:
            return True
        return origin.startswith(L.GROUP + ":") and item.get("about") == current_key
    if viewer == "group":
        return origin.startswith(L.GROUP + ":")
    return False


def ago(seconds):
    s = max(0, int(seconds))
    if s < HOUR:
        return f"{max(1, s // 60)} 分钟前"
    if s < 24 * HOUR:
        return f"{s // HOUR} 小时前"
    return f"{s // (24 * HOUR)} 天前"


class Mind:
    def __init__(self, path, settings, now=time.time, logger=None):
        self.path = path
        self.s = settings
        self.now = now
        self.log = logger
        self._lock = asyncio.Lock()
        self.state = self._load()

    # ---------- 持久化 ----------
    @staticmethod
    def _empty():
        return {"version": 1, "items": [], "sweep": {"last_ts": 0, "day": "", "count": 0, "slots": []}}

    def _load(self):
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            st = self._empty()
            if isinstance(data.get("items"), list):
                st["items"] = [i for i in data["items"] if isinstance(i, dict) and i.get("id")]
            if isinstance(data.get("sweep"), dict):
                st["sweep"].update({k: v for k, v in data["sweep"].items() if k in st["sweep"]})
            return st
        except Exception as e:
            bad = f"{self.path}.corrupt-{int(self.now())}"
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            if self.log:
                self.log.warning(f"[reach_out] 心事文件损坏（{type(e).__name__}），已移到 {bad}，从空白开始")
            return self._empty()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _prune(self):
        now = self.now()
        keep = []
        for i in self.state["items"]:
            if i.get("status") == ACTIVE and now >= float(i.get("expires_at", 0)):
                i["status"] = "expired"
                i["closed_at"] = now
            # 了结的留 3 天给 /心事 看，之后删掉
            if i.get("status") in FINISHED and now - float(i.get("closed_at", now)) > 3 * 24 * HOUR:
                continue
            keep.append(i)
        self.state["items"] = keep

    def _active(self):
        return [i for i in self.state["items"] if i.get("status") == ACTIVE]

    def _evict(self, pool, limit, now):
        """超出上限时挤掉最不急的：没有到期时间的先走，再按记下的时间从旧到新。"""
        while len(pool) > limit:
            victim = min(pool, key=lambda i: (i.get("due_at") is not None, float(i.get("created_at", 0))))
            victim["status"] = "dropped"
            victim["closed_at"] = now
            victim["close_note"] = "超出上限被挤掉"
            pool.remove(victim)

    # ---------- 记 / 了结 ----------
    async def keep(self, kind, about, about_name, origin, text, due_hours=None, source="self"):
        """返回 (条目副本, 是否合并到已有条目)。参数由调用方先校验。"""
        async with self._lock:
            self._prune()
            now = self.now()
            due_at = now + due_hours * HOUR if due_hours is not None else None
            if due_at is not None:
                expires = min(due_at + 24 * HOUR, now + self.s.mind_max_hours * HOUR)
            else:
                expires = now + self.s.mind_default_hours * HOUR
            for i in self._active():
                if i["about"] == about and i["kind"] == kind and similar(i["text"], text):
                    i.update({"text": text, "updated_at": now, "expires_at": max(float(i["expires_at"]), expires)})
                    if due_at is not None:
                        i["due_at"] = due_at
                        i["due_surfaced"] = False
                    if about_name:
                        i["about_name"] = about_name
                    self._save()
                    return dict(i), True
            item = {
                "id": "m" + uuid.uuid4().hex[:5], "kind": kind, "about": about, "about_name": about_name or "",
                "origin": origin, "text": text, "due_at": due_at, "expires_at": expires,
                "status": ACTIVE, "created_at": now, "updated_at": now,
                "surfaced": 0, "last_surfaced_at": 0, "due_surfaced": False, "source": source,
            }
            self.state["items"].append(item)
            self._evict([i for i in self._active() if i["about"] == about], self.s.mind_max_per_about, now)
            self._evict(self._active(), self.s.mind_max_total, now)
            self._save()
            return dict(item), False

    async def let_go(self, item_id, how):
        async with self._lock:
            self._prune()
            for i in self.state["items"]:
                if i.get("id") == item_id:
                    if i.get("status") != ACTIVE:
                        return None, "这件事已经了结了"
                    i["status"] = "done" if how == "done" else "dropped"
                    i["closed_at"] = self.now()
                    self._save()
                    return dict(i), None
            return None, f"没有 {item_id} 这件心事"

    async def items(self, include_finished=False):
        async with self._lock:
            self._prune()
            return [dict(i) for i in self.state["items"] if include_finished or i.get("status") == ACTIVE]

    async def has_similar(self, about, text):
        async with self._lock:
            return any(i["about"] == about and similar(i["text"], text) for i in self._active())

    # ---------- 注入 ----------
    async def pick(self, current_key, speaker_key, viewer, partners, limit):
        """挑出这一轮要注入的心事，并记为“提起过一次”。返回条目副本（含注入前的状态）。"""
        async with self._lock:
            self._prune()
            now = self.now()
            gap = self.s.mind_resurface_hours * HOUR
            chosen = []

            def eligible(i):
                if not visible(i, current_key, viewer, partners):
                    return None
                due = i.get("due_at")
                if due is not None and now >= float(due) and not i.get("due_surfaced"):
                    return "due"
                # 刚在这个会话里记下的，这里的上下文本来就有，不重复提
                if i.get("origin") == current_key and now - float(i.get("created_at", now)) < 4 * HOUR:
                    return None
                if int(i.get("surfaced", 0)) >= self.s.mind_max_surfaced:
                    return None
                last = float(i.get("last_surfaced_at", 0))
                if last and now - last < gap:
                    return None
                return "normal"

            ranked = []
            for i in self._active():
                why = eligible(i)
                if not why:
                    continue
                if i["about"] == speaker_key:
                    rank = 0
                elif i["about"] == current_key:
                    rank = 1
                elif why == "due":
                    rank = 2
                else:
                    continue  # 只提和眼前有关的、或已到期的
                ranked.append((rank, float(i.get("due_at") or 9e18), float(i["created_at"]), i, why))
            ranked.sort(key=lambda x: x[:3])
            for _, _, _, i, why in ranked[:limit]:
                snap = dict(i)
                snap["_why"] = why
                chosen.append(snap)
                i["surfaced"] = int(i.get("surfaced", 0)) + 1
                i["last_surfaced_at"] = now
                if why == "due":
                    i["due_surfaced"] = True
            if chosen:
                self._save()
            return chosen

    # ---------- 补漏记账 ----------
    async def sweep_slot(self, day, slot):
        """这个时段还能不能补漏；能的话先占上（每天上限、每个时段一次）。"""
        async with self._lock:
            sw = self.state["sweep"]
            if sw.get("day") != day:
                sw.update({"day": day, "count": 0, "slots": []})
            if slot in sw["slots"] or sw["count"] >= self.s.sweep_daily_max:
                return False
            sw["slots"].append(slot)
            sw["count"] += 1
            self._save()
            return True

    async def sweep_since(self):
        async with self._lock:
            return float(self.state["sweep"].get("last_ts") or 0)

    async def sweep_done(self, ts):
        async with self._lock:
            self.state["sweep"]["last_ts"] = ts
            self._save()


def render(items, now, place_of):
    """把挑出来的心事排成几行。place_of(key) -> 地点说明。"""
    lines = []
    for i in items:
        who = i.get("about_name") or i.get("about", "")
        when = ""
        if i.get("due_at") is not None:
            d = float(i["due_at"]) - now
            when = "，到时候了" if d <= 0 else f"，约 {max(1, round(d / HOUR))} 小时后"
        told = "还没提过" if not int(i.get("surfaced", 0)) else f"之前提起过 {i['surfaced']} 次"
        src = "（你回头想起来的）" if i.get("source") == "sweep" else ""
        lines.append(f"- [{i['id']}] {i['kind']}·{who}：{i['text']}"
                     f"（{ago(now - float(i['created_at']))}{place_of(i.get('origin', ''))}记下{when}；{told}）{src}")
    return lines


# ---------- 补漏：提示词与结果校验 ----------
SWEEP_SYSTEM = (
    "你在帮一个叫伽拉忒亚的聊天伙伴回顾她最近的聊天，找出她可能该惦记、但还没记下的事。"
    "只找两类：牵挂（对方提到的、值得之后关心一下的事，比如考试、生病、搬家、面试）和想分享"
    "（她自己说过想告诉谁的事）。不要找约定，不要编，玩笑和泛泛而谈不算。"
    "只输出 JSON 数组，最多 3 条，没有就输出 []。每条："
    '{"kind":"牵挂|想分享","origin":"会话键","about":"user:号码 或 group:号码","text":"一句话，30 字以内",'
    '"due_in_hours":数字或null}'
)


def sweep_prompt(entries):
    """entries: 近况条目（带 key/uid/who/text/ts）。按会话分组列出，并列出每个会话里允许的 about。"""
    groups = {}
    for e in entries:
        groups.setdefault(e.get("key", ""), []).append(e)
    parts = []
    for key, es in groups.items():
        abouts = {key}
        for e in es:
            if e.get("uid"):
                abouts.add(f"{L.USER}:{e['uid']}")
        parts.append(f"会话 {key}（about 只能从这些里选：{', '.join(sorted(abouts))}）")
        for e in es:
            uid = f"[{e['uid']}]" if e.get("uid") else ""
            parts.append(f"  {L.hm(e.get('ts', 0))} {e.get('who', '')}{uid}：{e.get('text', '')}")
    return "\n".join(parts)


def parse_sweep(text, entries, max_text):
    """校验模型输出：origin 必须是给出的会话，about 必须在那个会话允许的范围里。"""
    allowed = {}
    for e in entries:
        key = e.get("key", "")
        allowed.setdefault(key, {key})
        if e.get("uid"):
            allowed[key].add(f"{L.USER}:{e['uid']}")
    m = re.search(r"\[.*\]", str(text or ""), re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        kind = normalize_kind(d.get("kind"))
        origin, about = str(d.get("origin", "")), str(d.get("about", ""))
        t, err = L.clean_text(d.get("text"), max_text)
        if kind not in ("牵挂", "想分享") or err or origin not in allowed or about not in allowed[origin]:
            continue
        hours = d.get("due_in_hours")
        h, herr = parse_hours("" if hours is None else hours, 24 * 14)
        out.append({"kind": kind, "origin": origin, "about": about, "text": t, "due_hours": None if herr else h})
        if len(out) >= 3:
            break
    return out
