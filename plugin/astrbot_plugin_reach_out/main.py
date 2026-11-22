# -*- coding: utf-8 -*-
"""主动联系：让她能在任何会话里私信搭档，并给搭档授权过的人/群主动发消息。

- 搭档 = AstrBot 配置里的管理员（admins_id）。私信搭档不需要授权，但有频率上限。
- 其他人/群必须先由搭档用 `/授权` 加进白名单（可设有效期、条数），白名单外一律拒绝。
- 发出的消息记一条“最近外发”，之后在那个会话里请求模型时作为临时提示注入，
  让她记得自己主动说过什么；不改对话历史，避免和正在进行的回合抢写。
- 发送失败如实返回，额度退回；工具结果里明确写“没发出去”。
"""
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, register

try:
    from . import logic as L
except ImportError:  # 测试里按文件加载
    import logic as L

PLUGIN_NAME = "astrbot_plugin_reach_out"


def _fmt_time(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


@register(PLUGIN_NAME, "dafeiyu-tools", "主动联系：私信搭档；给搭档授权的人/群主动发消息", "1.0.0")
class ReachOutPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.settings = L.Settings.from_config(config)
        self.store = L.Store(os.path.join(self._data_dir(), "state.json"), self.settings, logger=logger)

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
    def _where(event):
        gid = ""
        try:
            gid = event.get_group_id() or ""
        except Exception:
            pass
        if gid:
            return f"群 {gid}"
        name = ""
        try:
            name = event.get_sender_name() or ""
        except Exception:
            pass
        sid = event.get_sender_id()
        return f"和 {name}({sid}) 私聊" if name else f"和 {sid} 私聊"

    async def _send(self, umo, text):
        """返回错误说明；成功返回 None。"""
        try:
            ok = await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:
            logger.warning(f"[reach_out] 发送到 {umo} 失败：{type(e).__name__}: {e}")
            return f"{type(e).__name__}: {str(e)[:120]}"
        if ok is False:
            return "找不到对应的消息平台"
        return None

    # ---------- 工具 ----------
    async def do_message_partner(self, event, text):
        s, err = L.clean_text(text, self.settings.max_text_len)
        if err:
            return f"没发出去：{err}。"
        partners = self._partners()
        if not partners:
            return "没发出去：还没有设置搭档（AstrBot 管理员列表是空的）。"
        platform = self._platform(event)
        here = event.unified_msg_origin
        targets = [p for p in partners if L.build_umo(platform, L.USER, p) != here]
        if not targets:
            return "你现在就在和搭档私聊，直接回复就行，不用另外私信。"
        token, err = await self.store.reserve_partner(targets, s)
        if err:
            return f"没发出去：{err}。"
        where = self._where(event)
        sent, failed = [], []
        for p in targets:
            umo = L.build_umo(platform, L.USER, p)
            e = await self._send(umo, s)
            if e:
                failed.append(f"{p}（{e}）")
            else:
                sent.append(p)
                await self.store.add_note(umo, f"（在{where}时主动私信你）{s}")
        if not sent:
            await self.store.refund(token)
            return f"没发出去：发送失败 {'；'.join(failed)}。不要说已经发了。"
        logger.info(f"[reach_out] 私信搭档 {sent}，来自 {where}，{len(s)} 字")
        msg = "已私信搭档。"
        if failed:
            msg += f" 但有一位没发成功：{'；'.join(failed)}。"
        return msg

    async def do_message_contact(self, event, kind, target_id, text):
        k = L.normalize_kind(kind)
        if not k:
            return "没发出去：kind 只能是 user（私聊）或 group（群）。"
        tid = L.normalize_id(target_id)
        if not tid:
            return "没发出去：号码不对，应为 5～12 位数字。"
        s, err = L.clean_text(text, self.settings.max_text_len)
        if err:
            return f"没发出去：{err}。"
        if k == L.USER and tid in self._partners():
            return "这是搭档，用 message_partner 私信搭档就行。"
        platform = self._platform(event)
        umo = L.build_umo(platform, k, tid)
        if umo == event.unified_msg_origin:
            return "你就在这个会话里，直接回复就行。"
        token, err = await self.store.reserve_contact(k, tid, s)
        if err:
            return f"没发出去：{err}。"
        e = await self._send(umo, s)
        if e:
            await self.store.refund(token)
            return f"没发出去：发送失败（{e}）。不要说已经发了。"
        await self.store.commit(token)
        g = token.get("grant_snapshot") or {}
        label = f"{L.kind_label(k)} {tid}" + (f"（{g.get('note')}）" if g.get("note") else "")
        where = self._where(event)
        await self.store.add_note(umo, f"（在{where}时主动发来）{s}")
        logger.info(f"[reach_out] 主动发给 {label}，来自 {where}，{len(s)} 字")
        if self.settings.cc_partner:
            cc = f"（抄送）刚在{where}时给{label}发了：{s}"
            for p in self._partners():
                pumo = L.build_umo(platform, L.USER, p)
                if pumo == event.unified_msg_origin:
                    continue  # 搭档本人就在当前会话里看着
                if await self._send(pumo, cc) is None:
                    await self.store.add_note(pumo, cc)
        rem = g.get("remaining")
        tail = f"，这个授权还剩 {rem} 条" if rem is not None else ""
        return f"已发给{label}{tail}。"

    async def do_list_contacts(self, event):
        grants = await self.store.list_grants()
        lines = [f"搭档：{len(self._partners())} 位（用 message_partner 私信，不需要授权）"]
        if not grants:
            lines.append("授权名单是空的：现在不能主动给其他人或群发消息，需要的话先问搭档。")
            return "\n".join(lines)
        lines.append("已授权（号码只用来调用工具，不要在群里念出来）：")
        for g in sorted(grants, key=lambda x: x.get("granted_at", 0)):
            exp = g.get("expires_at")
            rem = g.get("remaining")
            lines.append(
                f"- {L.kind_label(g['kind'])} {g['id']}"
                + (f"（{g['note']}）" if g.get("note") else "")
                + f"，{'永久' if exp is None else _fmt_time(exp) + ' 到期'}"
                + f"，{'不限条数' if rem is None else f'剩 {rem} 条'}"
            )
        return "\n".join(lines)

    @filter.llm_tool(name="message_partner")
    async def message_partner(self, event: AstrMessageEvent, text: str):
        """私信搭档（管理员）。在任何会话里都能用：遇到觉得该让搭档知道的事、有人找搭档、或想跟搭档说点什么时用。私聊里正在和搭档说话时直接回复，不用这个。只发纯文字，不用表情标签。返回“已私信”才算发了。

        Args:
            text(string): 要发给搭档的话，写清楚是哪里、谁、什么事
        """
        return await self.do_message_partner(event, text)

    @filter.llm_tool(name="message_contact")
    async def message_contact(self, event: AstrMessageEvent, kind: str, target_id: str, text: str):
        """给搭档授权过的人私聊或往授权过的群里发消息。不在授权名单里的一律发不出去，先用 list_contacts 看名单；没授权就用 message_partner 问搭档。只发纯文字，不用表情标签。返回“已发给”才算发了。

        Args:
            kind(string): user 表示私聊某人，group 表示发到群里
            target_id(string): 对方的 QQ 号或群号
            text(string): 要发的话
        """
        return await self.do_message_contact(event, kind, target_id, text)

    @filter.llm_tool(name="list_contacts")
    async def list_contacts(self, event: AstrMessageEvent):
        """查看能主动联系谁：搭档和授权名单（有效期、剩余条数）。"""
        return await self.do_list_contacts(event)

    # ---------- 提示她最近主动发过什么 ----------
    @filter.on_llm_request()
    async def inject_recent_outbound(self, event: AstrMessageEvent, req) -> None:
        try:
            notes = await self.store.notes_for(event.unified_msg_origin)
            if not notes:
                return
            lines = ["【你最近主动发到这个会话的消息】（对方的回复可能是在接这些话）"]
            for n in notes:
                lines.append(f"- {_fmt_time(n['ts'])} {n['text']}")
            req.system_prompt = f"{req.system_prompt or ''}\n\n" + "\n".join(lines) + "\n"
        except Exception as e:  # 注入失败不影响对话
            logger.warning(f"[reach_out] 注入最近外发失败：{type(e).__name__}: {e}")

    # ---------- 搭档命令 ----------
    @staticmethod
    def _args(event):
        parts = (event.message_str or "").split()
        return parts[1:] if parts else []

    async def do_grant(self, event):
        g, err = L.parse_grant_args(self._args(event), self.settings.default_grant_days)
        if err:
            return err
        if g["kind"] == L.USER and g["id"] in self._partners():
            return "这是搭档，本来就能私信，不用授权。"
        saved = await self.store.grant(g["kind"], g["id"], g["note"], g["days"], g["count"], by=event.get_sender_id())
        exp = saved.get("expires_at")
        rem = saved.get("remaining")
        return (
            f"已授权：{L.kind_label(g['kind'])} {g['id']}"
            + (f"（{g['note']}）" if g["note"] else "")
            + f"，{'永久' if exp is None else _fmt_time(exp) + ' 到期'}"
            + f"，{'不限条数' if rem is None else f'{rem} 条'}。"
            + ("\n提醒：对方不是她的 QQ 好友时私聊可能发不出去。" if g["kind"] == L.USER else "")
        )

    async def do_revoke(self, event):
        args = self._args(event)
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
