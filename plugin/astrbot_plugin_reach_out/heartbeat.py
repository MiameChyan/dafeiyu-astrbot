# -*- coding: utf-8 -*-
"""心跳：没人找她时，她偶尔会想起某件心事，自己去找人说。

纯逻辑，不依赖 AstrBot。流程（main.py 里串起来）：
  定时醒来（北京时间窗口内，每 interval 分钟一个时段，每个时段只跑一次）
  → 刹车（不调模型）：今日上限、距上次间隔、对象正在聊、对象连续没回、没有可说的心事
  → 问便宜模型要不要去（默认不去）
  → 去：用念头机制到对象私聊开口；之后 ignore_hours 内对象没说话记一次“没回”。
群不进心跳（群里暖场归 group_chat_plus）。
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime

try:
    from . import logic as L
    from . import mind as M
except ImportError:  # 测试里按文件加载
    import logic as L
    import mind as M

HOUR = 3600


def parse_window(text):
    """'09:30-23:30' -> (570, 1410) 分钟；不合法返回默认。"""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*[-~～]\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if m:
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        if 0 <= a < b <= 24 * 60:
            return a, b
    return 9 * 60 + 30, 23 * 60 + 30


def slot_of(now, window, interval_minutes):
    """返回 (日期, 时段序号)；不在窗口里返回 None。时间按北京时间。"""
    bj = datetime.fromtimestamp(now, L.SHANGHAI)
    minute = bj.hour * 60 + bj.minute
    start, end = window
    if not (start <= minute < end):
        return None
    return bj.strftime("%Y-%m-%d"), (minute - start) // max(1, int(interval_minutes))


def candidates(items, target_key, viewer, partners, now, limit=5):
    """可以拿来开口的心事：关于这个对象、对象私聊里能看到的，并且
    已到期 / 还没提过的想分享 / 挂了 4 小时以上还没提过的；心跳 24 小时内试过的跳过。"""
    out = []
    for i in items:
        if i.get("status") != M.ACTIVE or i.get("about") != target_key:
            continue
        if not M.visible(i, target_key, viewer, partners):
            continue
        if now - float(i.get("hb_tried_at") or 0) < 24 * HOUR:
            continue
        due = i.get("due_at")
        fresh = not int(i.get("surfaced", 0))  # 已经注入过的，她有过机会说，心跳不再重复（到期例外）
        ok = ((due is not None and now >= float(due))
              or (fresh and i.get("kind") == "想分享")
              or (fresh and now - float(i.get("created_at", now)) >= 4 * HOUR))
        if ok:
            out.append(i)
    out.sort(key=lambda i: (0 if i.get("due_at") is not None and now >= float(i["due_at"]) else 1,
                            float(i.get("created_at", 0))))
    return out[:limit]


JUDGE_SYSTEM = (
    "你在替一个叫伽拉忒亚的聊天伙伴判断：她现在一个人待着，要不要主动去私聊找某个人，说一件她惦记着的事。"
    "大多数时候答案是不去：没有值得说的、对方可能在忙、刚聊过、时间不合适，都不去。"
    "不要为了刷存在感而去，不要卖惨、不要讨关注。只在这件事现在说正合适时才去。"
    '只输出 JSON：{"act":true或false,"item_id":"候选里的编号","intent":"过去想说什么、为什么，一两句",'
    '"reason":"为什么去或不去，一句"}'
)


def judge_prompt(now, target_label, done_today, daily_max, cands, recent_lines):
    bj = datetime.fromtimestamp(now, L.SHANGHAI)
    week = "一二三四五六日"[bj.weekday()]
    lines = [f"现在是北京时间 {bj.strftime('%m-%d %H:%M')}，星期{week}。",
             f"对象：{target_label}。今天她已经主动找过人 {done_today} 次（上限 {daily_max}）。",
             "她惦记着的事（候选）："]
    for i in cands:
        due = ""
        if i.get("due_at") is not None:
            d = float(i["due_at"]) - now
            due = "，已到时候" if d <= 0 else f"，约 {max(1, round(d / HOUR))} 小时后到时候"
        lines.append(f"- [{i['id']}] {i['kind']}：{i['text']}（{M.ago(now - float(i['created_at']))}记下{due}；"
                     f"提起过 {int(i.get('surfaced', 0))} 次）")
    lines.append("她最近的经历：" if recent_lines else "她最近没和人聊过什么。")
    lines.extend(recent_lines)
    return "\n".join(lines)


def parse_judge(text, cand_ids, max_len):
    """返回 (决定 dict, 错误)。决定：{act, item_id, intent, reason}。"""
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    if not m:
        return None, "模型没给出 JSON"
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None, "模型的 JSON 坏了"
    if not isinstance(d, dict):
        return None, "模型输出不是对象"
    reason = L.clip(d.get("reason") or "", 60)
    if d.get("act") is not True:
        return {"act": False, "reason": reason or "模型说不去"}, None
    iid = str(d.get("item_id") or "")
    if iid not in cand_ids:
        return None, f"模型选的 {iid or '（空）'} 不在候选里"
    intent, err = L.clean_text(d.get("intent"), max_len)
    if err:
        return None, f"意图不合格：{err}"
    return {"act": True, "item_id": iid, "intent": intent, "reason": reason}, None


class Beat:
    """心跳状态：今天几次、上次什么时候、跑过的时段、每个对象的“没回”计数、7 天记录。"""

    def __init__(self, path, settings, now=time.time, logger=None):
        self.path = path
        self.s = settings
        self.now = now
        self.log = logger
        self._lock = asyncio.Lock()
        self.state = self._load()

    @staticmethod
    def _empty():
        return {"version": 1, "day": "", "count": 0, "slots": [], "last_act_ts": 0,
                "ignore": {}, "records": [], "platform_id": "", "self_id": ""}

    def _load(self):
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            st = self._empty()
            for k, v in data.items():
                if k in st and isinstance(v, type(st[k])):
                    st[k] = v
            return st
        except Exception as e:
            bad = f"{self.path}.corrupt-{int(self.now())}"
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            if self.log:
                self.log.warning(f"[reach_out] 心跳文件损坏（{type(e).__name__}），已移到 {bad}，从空白开始")
            return self._empty()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _roll_day(self, day):
        if self.state["day"] != day:
            self.state.update({"day": day, "count": 0, "slots": []})

    async def remember_platform(self, platform_id, self_id):
        async with self._lock:
            if platform_id and self_id and (self.state["platform_id"], self.state["self_id"]) != (platform_id, self_id):
                self.state["platform_id"], self.state["self_id"] = str(platform_id), str(self_id)
                self._save()

    async def take_slot(self, day, slot):
        async with self._lock:
            self._roll_day(day)
            if slot in self.state["slots"]:
                return False
            self.state["slots"].append(slot)
            self._save()
            return True

    async def record(self, result, reason="", target="", item_id=""):
        async with self._lock:
            now = self.now()
            self.state["records"] = [r for r in self.state["records"] if now - float(r.get("ts", 0)) < 7 * 24 * HOUR]
            self.state["records"].append({"ts": now, "result": result, "reason": L.clip(reason, 80),
                                          "target": target, "item_id": item_id})
            self._save()

    async def acted(self, day, target_key):
        async with self._lock:
            self._roll_day(day)
            now = self.now()
            self.state["count"] += 1
            self.state["last_act_ts"] = now
            ig = self.state["ignore"].setdefault(target_key, {"count": 0, "pending_since": 0, "paused_until": 0})
            ig["pending_since"] = now
            self._save()

    async def saw(self, target_key):
        """对象说话了：有待定的“没回”就清掉。每条消息都会调用，只有待定时才写盘。"""
        async with self._lock:
            ig = self.state["ignore"].get(target_key)
            if ig and (float(ig.get("pending_since") or 0) or int(ig.get("count", 0))):
                ig.update({"count": 0, "pending_since": 0})
                self._save()

    async def settle_ignores(self):
        """到了 ignore_hours 还没被 saw() 清掉的待定项，记一次“没回”；连续够数就暂停一天。"""
        async with self._lock:
            now = self.now()
            changed = False
            for key, ig in self.state["ignore"].items():
                since = float(ig.get("pending_since") or 0)
                if since and now - since >= self.s.heartbeat_ignore_hours * HOUR:
                    ig["count"] = int(ig.get("count", 0)) + 1
                    ig["pending_since"] = 0
                    if ig["count"] >= self.s.heartbeat_ignore_limit:
                        ig["paused_until"] = now + 24 * HOUR
                        ig["count"] = 0
                    changed = True
            if changed:
                self._save()

    async def snapshot(self):
        async with self._lock:
            return json.loads(json.dumps(self.state))


def brake_reason(state, now, settings, day):
    """全局刹车：今日上限、距上次间隔。返回原因或 None。"""
    count = state["count"] if state.get("day") == day else 0
    if count >= settings.heartbeat_daily_max:
        return f"今天已经主动找过 {count} 次"
    last = float(state.get("last_act_ts") or 0)
    if last and now - last < settings.heartbeat_min_gap_hours * HOUR:
        return f"距上次主动不到 {settings.heartbeat_min_gap_hours} 小时"
    return None


def target_reason(state, target_key, now, recent, settings):
    """单个对象的刹车：连续没回暂停中、正在聊。返回原因或 None。"""
    ig = (state.get("ignore") or {}).get(target_key) or {}
    if now < float(ig.get("paused_until") or 0):
        return "对方连着没回，先停一天"
    uid = target_key.split(":", 1)[1]
    busy = settings.heartbeat_busy_minutes * 60
    for e in recent:
        if (e.get("key") == target_key or e.get("uid") == uid) and now - float(e.get("ts", 0)) < busy:
            return "刚和对方聊过"
    return None


def fmt_records(records, now):
    if not records:
        return "今天还没有心跳记录。"
    day = datetime.fromtimestamp(now, L.SHANGHAI).strftime("%Y-%m-%d")
    lines = []
    for r in records:
        if datetime.fromtimestamp(float(r["ts"]), L.SHANGHAI).strftime("%Y-%m-%d") != day:
            continue
        tail = f" → {r['target']}" if r.get("target") else ""
        item = f" [{r['item_id']}]" if r.get("item_id") else ""
        lines.append(f"{L.hm(r['ts'])} {r['result']}{tail}{item}：{r.get('reason', '')}")
    return "\n".join(lines) if lines else "今天还没有心跳记录。"
