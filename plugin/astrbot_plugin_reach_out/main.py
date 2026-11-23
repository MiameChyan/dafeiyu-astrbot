# -*- coding: utf-8 -*-
"""主动联系 v2：走过去说 + 近况 + 对话里授权。

- go_talk：她决定去哪说话时调用。插件不替她发文字，而是在目标会话里构造一条“她自己的念头”，
  送进 AstrBot 正常流水线（platform.create_event → commit_event）。她在那边看着那边的聊天记录
  自己开口，分条、表情转换、表情包工具、写历史、LivingMemory 都和平常一样。
  群里的念头带一个 @她 的组件，AstrBot 原生唤醒；私聊本来就不需要唤醒词。
- 去搭档私聊随时可以（有频率上限）；其他人/群必须在搭档授权的白名单里。
- grant_contact / revoke_contact：搭档在对话里说一声就能授权；代码只认搭档本人发起的那一轮。
- 近况：记下她在各处“谁说了什么 → 我回了什么”，按隐私规则注入到别的会话。
  她的发言取自核心补丁的出站回调（只有真正发送成功的才算）。
- 念头那一轮不能再 go_talk、不能授权（防循环、防借念头提权）。
"""
import asyncio
import os
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, register

try:
    from . import logic as L
    from . import mind as M
except ImportError:  # 测试里按文件加载
    import logic as L
    import mind as M

PLUGIN_NAME = "astrbot_plugin_reach_out"
THOUGHT_EXTRA = "reach_out_thought"
THOUGHT_NAME = "伽拉忒亚的念头"
PARTNER_WORDS = {"partner", "搭档", "admin"}


def _load_hooks():
    try:
        from astrbot.core import dafeiyu_hooks

        return dafeiyu_hooks
    except Exception:
        return None


@register(PLUGIN_NAME, "dafeiyu-tools", "主动联系：走过去说、近况、对话里授权", "2.0.0")
class ReachOutPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.settings = L.Settings.from_config(config)
        data_dir = self._data_dir()
        self.store = L.Store(os.path.join(data_dir, "state.json"), self.settings, logger=logger)
        self.mind = M.Mind(os.path.join(data_dir, "mind.json"), self.settings, logger=logger)
        self._sweeper = None
        self._tasks = set()
        self._names = {}  # "group:id"/"user:id" -> 显示名（从入站事件里顺手记下）
        self._sleep = asyncio.sleep
        self._undo = []
        hooks = _load_hooks()
        if hooks is not None and callable(getattr(hooks, "register_outbound", None)):
            self._undo.append(hooks.register_outbound(self.on_outbound))
        else:
            logger.warning("[reach_out] 没有出站挂钩（核心补丁未挂载），近况里不会记她说了什么")

    async def terminate(self):
        for undo in self._undo:
            try:
                undo()
            except Exception:
                pass
        self._undo.clear()
        for t in list(self._tasks):
            t.cancel()
        if self._sweeper:
            self._sweeper.cancel()

    # ---------- 环境 ----------
    @staticmethod
    def _data_dir():
        try:
            from astrbot.api.star import StarTools

            return str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                base = get_astrbot_data_path()
            except Exception:
                base = os.path.join(os.getcwd(), "data")
            d = os.path.join(base, "plugin_data", PLUGIN_NAME)
            os.makedirs(d, exist_ok=True)
            return d

    def _partners(self):
        try:
            ids = self.context.get_config().get("admins_id") or []
        except Exception:
            ids = []
        out = []
        for a in ids:
            tid = L.normalize_id(a)
            if tid and tid not in out:
                out.append(tid)
        return out

    def _platform(self, event):
        return self.settings.platform_id or event.get_platform_id()

    @staticmethod
    def _is_thought(event):
        try:
            return bool(event.get_extra(THOUGHT_EXTRA))
        except Exception:
            return False

    @staticmethod
    def _gid(event):
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    def _key(self, event):
        gid = self._gid(event)
        return L.target_key(L.GROUP, gid) if gid else L.target_key(L.USER, str(event.get_sender_id()))

    def _place_of(self, kind, tid):
        if kind == L.GROUP:
            name = self._names.get(L.target_key(L.GROUP, tid))
            return f"群「{name}」({tid})" if name else f"群 {tid}"
        if tid in self._partners():
            return "和搭档私聊"
        name = self._names.get(L.target_key(L.USER, tid))
        return f"和{name}({tid})私聊" if name else f"和 {tid} 私聊"

    def _where(self, event):
        gid = self._gid(event)
        if gid:
            return f"在{self._place_of(L.GROUP, gid)}里"
        return f"{self._place_of(L.USER, str(event.get_sender_id()))}时"

    def _remember_names(self, event):
        try:
            gid = self._gid(event)
            if gid:
                grp = getattr(event.message_obj, "group", None)
                gname = getattr(grp, "group_name", None) if grp else None
                if gname:
                    self._names[L.target_key(L.GROUP, gid)] = L.clip(gname, 20)
            else:
                name = event.get_sender_name()
                if name:
                    self._names[L.target_key(L.USER, str(event.get_sender_id()))] = L.clip(name, 16)
        except Exception:
            pass

    def _is_partner_turn(self, event):
        """代码层面的“搭档本人发起的这一轮”：不是念头、平台判定为管理员、发送者在 admins_id 里。"""
        if self._is_thought(event):
            return False
        return getattr(event, "role", "") == "admin" and str(event.get_sender_id()) in self._partners()

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---------- 走过去说 ----------
    def build_thought(self, platform, kind, tid, intent, where, self_id):
        """构造目标会话里的一条“念头”消息，返回 (event, 错误)。"""
        from astrbot.api.message_components import At, Plain
        from astrbot.api.platform import AstrBotMessage, Group, MessageMember, MessageType

        inst = self.context.get_platform_inst(platform)
        if inst is None:
            return None, "找不到对应的消息平台"
        here = "这个群" if kind == L.GROUP else "这里"
        text = (f"（这是你自己的念头，不是谁发来的消息）你刚才{where}，决定来{here}：{intent}\n"
                "就像平常一样，看着这里的聊天直接开口说；不要提起这条念头本身。")
        abm = AstrBotMessage()
        abm.self_id = str(self_id)
        abm.message_id = f"reach_out-{uuid.uuid4().hex[:12]}"
        abm.timestamp = int(time.time())
        abm.session_id = tid
        abm.message_str = text
        if kind == L.GROUP:
            sender_id = self.settings.thought_sender_id
            abm.type = MessageType.GROUP_MESSAGE
            abm.group = Group(tid)
            abm.message = [At(qq=str(self_id)), Plain(text=text)]
            raw_type = "group"
        else:
            sender_id = tid  # 私聊会话由发送者决定，只能是对方本人；靠念头文字和身份标记说明不是对方说的
            abm.type = MessageType.FRIEND_MESSAGE
            abm.message = [Plain(text=text)]
            raw_type = "private"
        abm.sender = MessageMember(str(sender_id), THOUGHT_NAME)
        abm.raw_message = {
            "post_type": "message", "message_type": raw_type, THOUGHT_EXTRA: True,
            "self_id": str(self_id), "user_id": str(sender_id), "message_id": abm.message_id,
            "group_id": tid if kind == L.GROUP else None,
            "sender": {"user_id": str(sender_id), "nickname": THOUGHT_NAME},
            "message": [{"type": "text", "data": {"text": text}}],
        }
        ev = inst.create_event(abm)
        ev.set_extra(THOUGHT_EXTRA, True)
        # 念头永远不是管理员：私聊念头的发送者是对方本人（可能就是搭档），AstrBot 唤醒阶段
        # 会按号码给 admin；这个 extra 为 False 时不给（v4.29.0 waking_check）。
        ev.set_extra("_api_key_allow_admin_role", False)
        return (inst, ev), None

    async def _deliver(self, built, token, kind, tid, label, key_from, place_from):
        inst, ev = built
        try:
            await self._sleep(self.settings.thought_delay_seconds)
            inst.commit_event(ev)
        except asyncio.CancelledError:
            await self.store.refund(token)
            raise
        except Exception as e:
            logger.warning(f"[reach_out] 去{label}失败：{type(e).__name__}: {e}")
            await self.store.refund(token)
            await self.store.add_recent(key_from, place_from, "我", f"（想去{label}，但没去成）")
            return
        await self.store.commit(token)
        logger.info(f"[reach_out] 已在{label}唤起念头")

    async def do_go_talk(self, event, where, target_id, intent):
        if self._is_thought(event):
            return "这一轮本身就是你过来说话的念头，直接在这里说，不要再去别处。"
        s, err = L.clean_text(intent, self.settings.max_text_len)
        if err:
            return f"没去成：{err}。"
        partners = self._partners()
        w = str(where or "").strip().lower()
        if w in PARTNER_WORDS:
            if not partners:
                return "没去成：还没有设置搭档。"
            tid = L.normalize_id(target_id) if L.normalize_id(target_id) in partners else partners[0]
            kind = L.USER
        else:
            kind = L.normalize_kind(where)
            if not kind:
                return "没去成：where 只能是 partner（搭档私聊）、user（私聊某人）或 group（群）。"
            tid = L.normalize_id(target_id)
            if not tid:
                return "没去成：号码不对，应为 5～12 位数字。"
        is_partner = kind == L.USER and tid in partners
        platform = self._platform(event)
        if L.target_key(kind, tid) == self._key(event):
            return "你就在这里，直接说就行。"
        if is_partner:
            token, err = await self.store.reserve_partner([tid], s)
        else:
            token, err = await self.store.reserve_contact(kind, tid, s)
        if err:
            return f"没去成：{err}。"
        label = self._place_of(kind, tid)
        try:
            built, err = self.build_thought(platform, kind, tid, s, self._where(event), event.get_self_id())
        except Exception as e:
            built, err = None, f"{type(e).__name__}: {str(e)[:100]}"
        if err:
            await self.store.refund(token)
            return f"没去成：{err}。"
        key_from = self._key(event)
        place_from = self._place_of(*key_from.split(":", 1))
        await self.store.add_recent(key_from, place_from, "我", f"（决定去{label}：{L.clip(s, 60)}）")
        self._spawn(self._deliver(built, token, kind, tid, label, key_from, place_from))
        return (f"已动身去{label}。到了那边你会看着那边的聊天自己开口；"
                "这里不用把要说的话再说一遍，也别说“已经说了”——你还没到。")

    # ---------- 授权 ----------
    async def _grant(self, event, kind, tid, note, days, count):
        k = L.normalize_kind(kind)
        t = L.normalize_id(tid)
        if not k or not t:
            return "类型或号码不对：类型用 私聊/群，号码是 5～12 位数字。"
        if k == L.USER and t in self._partners():
            return "这是搭档，本来就能去，不用授权。"
        try:
            days = int(days) if str(days).strip() != "" else self.settings.default_grant_days
            count = int(count) if str(count).strip() != "" else 0
        except (TypeError, ValueError):
            return "天数和条数要是数字（0 = 永久 / 不限）。"
        if days < 0 or count < 0 or days > 3650 or count > 100000:
            return "天数或条数超出范围。"
        g = await self.store.grant(k, t, L.clip(note or "", 40), days, count, by=event.get_sender_id())
        exp, rem = g.get("expires_at"), g.get("remaining")
        return (f"已授权：{L.kind_label(k)} {t}" + (f"（{g['note']}）" if g.get("note") else "")
                + f"，{'永久' if exp is None else L.md_hm(exp) + ' 到期'}"
                + f"，{'不限条数' if rem is None else f'{rem} 次'}。"
                + ("\n提醒：对方不是她的 QQ 好友时私聊可能发不出去。" if k == L.USER else ""))

    async def do_grant_tool(self, event, kind, target_id, note="", days="", count=""):
        if not self._is_partner_turn(event):
            return "没授权：只有搭档本人跟你说，才能授权。"
        return await self._grant(event, kind, target_id, note, days, count)

    async def do_revoke_tool(self, event, kind, target_id):
        if not self._is_partner_turn(event):
            return "没撤销：只有搭档本人跟你说，才能撤销授权。"
        k, t = L.normalize_kind(kind), L.normalize_id(target_id)
        if not k or not t:
            return "类型或号码不对。"
        ok = await self.store.revoke(k, t)
        return f"已撤销 {L.kind_label(k)} {t} 的授权。" if ok else f"{L.kind_label(k)} {t} 本来就没有授权。"

    async def do_list_contacts(self, event):
        grants = await self.store.list_grants()
        lines = ["搭档的私聊：随时能去（go_talk where=partner）。"]
        if not grants:
            lines.append("授权名单是空的：别的人和群现在去不了，需要的话请搭档授权。")
            return "\n".join(lines)
        lines.append("可以去的地方（号码只用来调用工具，不要在群里念出来）：")
        for g in sorted(grants, key=lambda x: x.get("granted_at", 0)):
            exp, rem = g.get("expires_at"), g.get("remaining")
            lines.append(
                f"- {L.kind_label(g['kind'])} {g['id']}" + (f"（{g['note']}）" if g.get("note") else "")
                + f"，{'永久' if exp is None else L.md_hm(exp) + ' 到期'}"
                + f"，{'不限次数' if rem is None else f'剩 {rem} 次'}"
            )
        return "\n".join(lines)

    # ---------- 工具 ----------
    @filter.llm_tool(name="go_talk")
    async def go_talk(self, event: AstrMessageEvent, where: str, target_id: str, intent: str):
        """去另一个地方开口说话：搭档的私聊，或搭档授权过的私聊/群（用 list_contacts 查）。调用后你会“走过去”，在那边看着那边的聊天，像平常一样自己说出来（能分条、能发表情）。这里正常回一句就好，比如“好，我去说”；工具返回的是“已动身”，不是“已说完”。

        Args:
            where(string): partner 表示去搭档的私聊；user 表示私聊某人；group 表示去某个群
            target_id(string): 对方的 QQ 号或群号；去搭档私聊时可以留空
            intent(string): 你过去想说什么、为什么，写清楚来龙去脉，到了那边你会照着这个用自己的话说
        """
        return await self.do_go_talk(event, where, target_id, intent)

    @filter.llm_tool(name="list_contacts")
    async def list_contacts(self, event: AstrMessageEvent):
        """查看你能主动去哪里说话：搭档的私聊，以及搭档授权的私聊和群（有效期、剩余次数）。"""
        return await self.do_list_contacts(event)

    @filter.llm_tool(name="grant_contact")
    async def grant_contact(self, event: AstrMessageEvent, kind: str, target_id: str, note: str, days: str, count: str):
        """把某人的私聊或某个群加进你能主动去的名单。只有搭档本人明确让你授权时才用；别人说的不算，程序也会拒绝。

        Args:
            kind(string): 私聊 或 群
            target_id(string): QQ 号或群号
            note(string): 备注，比如群名、人名；没有就留空
            days(string): 有效天数，0 表示永久；留空用默认 7 天
            count(string): 最多去几次，0 表示不限；留空表示不限
        """
        return await self.do_grant_tool(event, kind, target_id, note, days, count)

    @filter.llm_tool(name="revoke_contact")
    async def revoke_contact(self, event: AstrMessageEvent, kind: str, target_id: str):
        """把某人的私聊或某个群从你能主动去的名单里移除。只有搭档本人让你撤销时才用。

        Args:
            kind(string): 私聊 或 群
            target_id(string): QQ 号或群号
        """
        return await self.do_revoke_tool(event, kind, target_id)

    # ---------- 心事 ----------
    def _resolve_about(self, event, about):
        """返回 (about 键, 显示名, 错误)。"""
        a = str(about or "").strip().lower()
        gid = self._gid(event)
        if a in ("here", "这里", "对方", ""):
            if self._is_thought(event) and gid:
                return None, None, "念头里没有说话人，about 用 group 或 user:号码"
            sid = str(event.get_sender_id())
            if sid in self._partners():
                return L.target_key(L.USER, sid), "搭档", None
            name = L.clip(event.get_sender_name() or "", 16) or sid
            return L.target_key(L.USER, sid), name, None
        if a in ("group", "群", "这个群"):
            if not gid:
                return None, None, "现在不在群里，about 不能用 group"
            return L.target_key(L.GROUP, gid), self._names.get(L.target_key(L.GROUP, gid)) or f"群 {gid}", None
        if a in PARTNER_WORDS:
            partners = self._partners()
            if not partners:
                return None, None, "还没有设置搭档"
            return L.target_key(L.USER, partners[0]), "搭档", None
        m = a.split(":", 1)
        if len(m) == 2:
            kind, tid = L.normalize_kind(m[0]), L.normalize_id(m[1])
            if kind and tid:
                key = L.target_key(kind, tid)
                if kind == L.USER and tid in self._partners():
                    return key, "搭档", None
                return key, self._names.get(key) or tid, None
        return None, None, "about 只能是 here、group、partner、user:号码 或 group:号码"

    async def do_keep_in_mind(self, event, kind, about, text, due_in_hours=""):
        k = M.normalize_kind(kind)
        if not k:
            return "没记下：kind 只能是 约定、牵挂 或 想分享。"
        t, err = L.clean_text(text, self.settings.mind_text_len)
        if err:
            return f"没记下：{err}。"
        key, name, err = self._resolve_about(event, about)
        if err:
            return f"没记下：{err}。"
        hours, err = M.parse_hours(due_in_hours, self.settings.mind_max_hours)
        if err:
            return f"没记下：{err}。"
        item, merged = await self.mind.keep(k, key, name, self._key(event), t, hours)
        return f"{'已更新' if merged else '记在心上了'}：[{item['id']}] {k}·{name}：{t}。办完了或者不用再挂着时，用 let_go。"

    async def do_let_go(self, event, item_id, how):
        h = str(how or "").strip().lower()
        if h not in ("done", "drop", "办了", "放下"):
            return "how 只能是 done（办了）或 drop（不用再挂着）。"
        item, err = await self.mind.let_go(str(item_id or "").strip(), "done" if h in ("done", "办了") else "drop")
        if err:
            return f"没改：{err}。"
        return f"好，[{item['id']}] {item['text']} {'办完了' if item['status'] == 'done' else '放下了'}。"

    def _mind_place(self, key):
        if not key or ":" not in key:
            return ""
        kind, tid = key.split(":", 1)
        return "在" + self._place_of(kind, tid) + ("里" if kind == L.GROUP else "时")

    async def do_list_mind(self):
        items = await self.mind.items(include_finished=True)
        if not items:
            return "她现在心里没挂着什么事。"
        now = self.mind.now()
        lines = []
        for i in sorted(items, key=lambda x: (x.get("status") != M.ACTIVE, x.get("created_at", 0))):
            state = "" if i["status"] == M.ACTIVE else f"〔{ {'done': '办了', 'dropped': '放下', 'expired': '过期'}[i['status']] }〕"
            due = f"，到期 {L.md_hm(i['due_at'])}" if i.get("due_at") else ""
            src = "，补漏" if i.get("source") == "sweep" else ""
            lines.append(f"{state}[{i['id']}] {i['kind']}·{i.get('about_name') or i['about']}：{i['text']}"
                         f"（{M.ago(now - float(i['created_at']))}{self._mind_place(i.get('origin'))}{due}，"
                         f"提起 {i.get('surfaced', 0)} 次{src}）")
        return "\n".join(lines)

    @filter.llm_tool(name="keep_in_mind")
    async def keep_in_mind(self, event: AstrMessageEvent, kind: str, about: str, text: str, due_in_hours: str):
        """把一件事挂在心上：答应了别人的事（约定）、在意的对方的事（牵挂，比如考试、生病、搬家）、想告诉某人的事（想分享）。之后和这个人说话时你会想起来。精确到点的提醒用定时任务，不用这个。

        Args:
            kind(string): 约定、牵挂 或 想分享
            about(string): 关于谁：here（正在跟你说话的这个人）、group（当前这个群）、partner（搭档）或 user:QQ号
            text(string): 一句话说清楚是什么事，80 字以内
            due_in_hours(string): 大概几小时后该想起，比如“明天”就是 24；不确定就留空
        """
        return await self.do_keep_in_mind(event, kind, about, text, due_in_hours)

    @filter.llm_tool(name="let_go")
    async def let_go(self, event: AstrMessageEvent, item_id: str, how: str):
        """一件挂在心上的事了结了：问过了、办完了用 done；不用再惦记了用 drop。

        Args:
            item_id(string): 心事的编号，比如 m3f2a（在【你心里挂着的事】里能看到）
            how(string): done 或 drop
        """
        return await self.do_let_go(event, item_id, how)

    # ---------- 补漏 ----------
    def _ensure_sweeper(self):
        if self._sweeper is None and self.settings.sweep_enabled:
            try:
                self._sweeper = asyncio.get_running_loop().create_task(self._sweep_loop())
            except RuntimeError:
                pass

    def _sweep_hours(self):
        out = set()
        for p in str(self.settings.sweep_hours or "").replace("，", ",").split(","):
            p = p.strip()
            if p.isdigit() and 0 <= int(p) <= 23:
                out.add(int(p))
        return out

    async def _sweep_loop(self):
        while True:
            try:
                await self.sweep_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[reach_out] 补漏出错：{type(e).__name__}: {e}")
            await asyncio.sleep(300)  # 不用 self._sleep：那个是给念头投递的延迟用的

    async def sweep_tick(self):
        """到了补漏时段（北京时间整点所在的那一小时）且这个时段还没做过，就做一次。"""
        from datetime import datetime

        now = self.mind.now()
        bj = datetime.fromtimestamp(now, L.SHANGHAI)
        if bj.hour not in self._sweep_hours():
            return False
        if not await self.mind.sweep_slot(bj.strftime("%Y-%m-%d"), bj.hour):
            return False
        await self.run_sweep()
        return True

    async def run_sweep(self):
        since = await self.mind.sweep_since()
        now = self.mind.now()
        entries = [e for e in await self.store.recent() if float(e.get("ts", 0)) > since]
        await self.mind.sweep_done(now)
        if not any(e.get("uid") for e in entries):
            return []  # 这段时间没人跟她说过话
        provider = self.context.get_provider_by_id(self.settings.sweep_provider_id)
        if provider is None:
            logger.warning(f"[reach_out] 补漏：找不到提供商 {self.settings.sweep_provider_id}")
            return []
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=M.sweep_prompt(entries), system_prompt=M.SWEEP_SYSTEM),
                timeout=self.settings.sweep_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("[reach_out] 补漏：模型超时")
            return []
        except Exception as e:
            logger.warning(f"[reach_out] 补漏：模型出错 {type(e).__name__}")
            return []
        cands = M.parse_sweep(getattr(resp, "completion_text", ""), entries, self.settings.mind_text_len)
        names = {f"{L.USER}:{e['uid']}": e.get("who") for e in entries if e.get("uid")}
        kept = []
        for c in cands:
            if await self.mind.has_similar(c["about"], c["text"]):
                continue
            kind, tid = c["about"].split(":", 1)
            name = "搭档" if kind == L.USER and tid in self._partners() else (
                names.get(c["about"]) or self._names.get(c["about"]) or tid)
            item, _ = await self.mind.keep(c["kind"], c["about"], name, c["origin"], c["text"],
                                           c["due_hours"], source="sweep")
            kept.append(item)
        logger.info(f"[reach_out] 补漏：看了 {len(entries)} 条近况，记下 {len(kept)} 件心事")
        return kept

    # ---------- 近况 ----------
    async def on_outbound(self, info):
        """核心补丁的出站回调：只有 QQ 真的发送成功才会调用。"""
        try:
            parts = []
            for comp in getattr(info.message_chain, "chain", None) or []:
                name = type(comp).__name__
                if name == "Plain":
                    parts.append(getattr(comp, "text", "") or "")
                elif name == "Image":
                    parts.append("[图片]")
                elif name == "Record":
                    parts.append("[语音]")
                elif name == "Face":
                    parts.append("[表情]")
            text = " ".join(p.strip() for p in parts if p and p.strip())
            if not text:
                return
            origin = getattr(info, "origin", "self") or "self"
            who = "我" if origin == "self" else f"我（{origin}）"
            kind = L.GROUP if info.is_group else L.USER
            sid = str(info.session_id)
            await self.store.add_recent(L.target_key(kind, sid), self._place_of(kind, sid), who, text,
                                        merge_seconds=30)
        except Exception as e:
            logger.warning(f"[reach_out] 记录出站近况失败：{type(e).__name__}: {e}")

    def _viewer(self, event):
        if self._gid(event):
            return "group"
        return "partner" if str(event.get_sender_id()) in self._partners() else "user"

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        self._ensure_sweeper()
        try:
            self._remember_names(event)
            key = self._key(event)
            entries = await self.store.recent()
            lines = L.render_recent(entries, key, self._viewer(event), self._partners(),
                                    max_lines=self.settings.recent_inject_max)
            if lines:
                block = ("【你最近在别处】（这是你自己的经历，可以自然提起；"
                         "“内容不在这里说”的私聊不要透露）\n" + "\n".join(lines))
                req.system_prompt = f"{req.system_prompt or ''}\n\n{block}\n"
            if not self._is_thought(event):
                sid = str(event.get_sender_id())
                who = "搭档" if sid in self._partners() else (L.clip(event.get_sender_name() or "", 16) or sid)
                text = L.strip_tag(event.message_str)
                kind, tid = key.split(":", 1)
                await self.store.add_recent(key, self._place_of(kind, tid), who, L.clip(text, 60), uid=sid)
        except Exception as e:  # 近况失败不影响对话
            logger.warning(f"[reach_out] 近况处理失败：{type(e).__name__}: {e}")
        try:
            key = self._key(event)
            speaker = None if (self._is_thought(event) and self._gid(event)) else \
                L.target_key(L.USER, str(event.get_sender_id()))
            items = await self.mind.pick(key, speaker, self._viewer(event), self._partners(),
                                         self.settings.mind_inject_max)
            if items:
                block = ("【你心里挂着的事】（你自己惦记着的；合适的时候自然提起，不用每次都提；"
                         "办完了或不用再挂着就 let_go）\n" + "\n".join(M.render(items, self.mind.now(), self._mind_place)))
                req.system_prompt = f"{req.system_prompt or ''}\n\n{block}\n"
        except Exception as e:  # 心事失败不影响对话
            logger.warning(f"[reach_out] 心事注入失败：{type(e).__name__}: {e}")

    # ---------- 搭档命令 ----------
    @staticmethod
    def _args(event, cmd):
        """取命令名之后的参数。message_str 前面可能被 face_convert 加了身份标记
        （如 "[搭档(QQ)] 授权 群 …"），所以按命令名定位，不按第一个词。"""
        parts = (event.message_str or "").split()
        for i, p in enumerate(parts):
            if p.lstrip("/") == cmd:
                return parts[i + 1:]
        return []

    async def do_grant(self, event):
        g, err = L.parse_grant_args(self._args(event, "授权"), self.settings.default_grant_days)
        if err:
            return err
        return await self._grant(event, g["kind"], g["id"], g["note"], g["days"], g["count"])

    async def do_revoke(self, event):
        args = self._args(event, "撤销授权")
        if len(args) < 2:
            return "用法：/撤销授权 私聊|群 <号码>"
        k, tid = L.normalize_kind(args[0]), L.normalize_id(args[1])
        if not k or not tid:
            return "用法：/撤销授权 私聊|群 <号码>"
        ok = await self.store.revoke(k, tid)
        return f"已撤销 {L.kind_label(k)} {tid} 的授权。" if ok else f"{L.kind_label(k)} {tid} 本来就没有授权。"

    @permission_type(PermissionType.ADMIN)
    @filter.command("授权")
    async def cmd_grant(self, event: AstrMessageEvent):
        """[搭档] /授权 私聊|群 <号码> [备注] [天数] [条数]"""
        yield event.plain_result(await self.do_grant(event))

    @permission_type(PermissionType.ADMIN)
    @filter.command("撤销授权")
    async def cmd_revoke(self, event: AstrMessageEvent):
        """[搭档] /撤销授权 私聊|群 <号码>"""
        yield event.plain_result(await self.do_revoke(event))

    @permission_type(PermissionType.ADMIN)
    @filter.command("心事")
    async def cmd_mind(self, event: AstrMessageEvent):
        """[搭档] 查看她心里挂着的事（含 3 天内了结的）"""
        yield event.plain_result(await self.do_list_mind())

    @permission_type(PermissionType.ADMIN)
    @filter.command("授权列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """[搭档] 查看主动联系的授权名单"""
        yield event.plain_result(await self.do_list_contacts(event))
