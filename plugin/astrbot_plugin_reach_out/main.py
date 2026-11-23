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
except ImportError:  # 测试里按文件加载
    import logic as L

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
        self.store = L.Store(os.path.join(self._data_dir(), "state.json"), self.settings, logger=logger)
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
                await self.store.add_recent(key, self._place_of(kind, tid), who, L.clip(text, 60))
        except Exception as e:  # 近况失败不影响对话
            logger.warning(f"[reach_out] 近况处理失败：{type(e).__name__}: {e}")

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
    @filter.command("授权列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """[搭档] 查看主动联系的授权名单"""
        yield event.plain_result(await self.do_list_contacts(event))
