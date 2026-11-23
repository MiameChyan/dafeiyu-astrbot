# -*- coding: utf-8 -*-
"""reach_out v2 回归：走过去说（构造念头进流水线）、近况与隐私、对话里授权、限流、持久化。

跑法（纯标准库，不需要 AstrBot、不联网）：python -m unittest tests.test_reach_out
原理：logic.py 直接测；插件入口用空壳 astrbot 模块 + 假 Context/假平台（记录 commit_event）。
号码全部是虚构的。
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugin", "astrbot_plugin_reach_out")
PARTNER = "10001"
SELF_ID = "90009"
PLATFORM = "qq"
GROUP_A = "500001"
GROUP_B = "500002"
FRIEND = "20002"
OTHER = "30003"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


L = _load("reach_out_logic", os.path.join(PLUGIN, "logic.py"))


def _stub(name, **attrs):
    m = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Star:
    def __init__(self, context=None, *a, **k):
        self.context = context


class _Filter:
    tools = {}
    commands = {}
    perms = {}

    @classmethod
    def llm_tool(cls, name=None, **k):
        def deco(f):
            cls.tools[name] = f
            return f
        return deco

    @classmethod
    def command(cls, name, **k):
        def deco(f):
            cls.commands[name] = f
            return f
        return deco

    @staticmethod
    def on_llm_request(*a, **k):
        return lambda f: f


class _PermissionType:
    ADMIN = "admin"


def _permission_type(level):
    def deco(f):
        _Filter.perms[f.__name__] = level
        return f
    return deco


class _Logger:
    def __init__(self):
        self.lines = []

    def info(self, m, *a):
        self.lines.append(str(m))

    debug = warning = error = info


# ---- 消息组件 / 平台类型的空壳 ----
class Plain:
    def __init__(self, text=""):
        self.text = text


class At:
    def __init__(self, qq="", name=""):
        self.qq = qq


class Image:
    pass


class _MessageType:
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"


class Group:
    def __init__(self, group_id):
        self.group_id = group_id
        self.group_name = None


class MessageMember:
    def __init__(self, user_id, nickname=""):
        self.user_id, self.nickname = user_id, nickname


class AstrBotMessage:
    def __init__(self):
        self.group = None


class _Hooks:
    outbound = []

    @classmethod
    def register_outbound(cls, fn):
        cls.outbound.append(fn)
        return lambda: cls.outbound.remove(fn) if fn in cls.outbound else None


LOG = _Logger()
for pkg in ("astrbot", "astrbot.api", "astrbot.core"):
    _stub(pkg)
_stub("astrbot.api", logger=LOG)
_stub("astrbot.api.event", AstrMessageEvent=object, filter=_Filter)
_stub("astrbot.api.event.filter", PermissionType=_PermissionType, permission_type=_permission_type)
_stub("astrbot.api.star", Context=object, Star=_Star, register=lambda *a, **k: (lambda c: c))
_stub("astrbot.api.message_components", Plain=Plain, At=At)
_stub("astrbot.api.platform", AstrBotMessage=AstrBotMessage, Group=Group, MessageMember=MessageMember,
      MessageType=_MessageType)
_stub("astrbot.core.dafeiyu_hooks", register_outbound=_Hooks.register_outbound)
sys.modules["astrbot.core"].dafeiyu_hooks = sys.modules["astrbot.core.dafeiyu_hooks"]
sys.modules["astrbot.api"].event = sys.modules["astrbot.api.event"]
sys.modules["astrbot.api"].star = sys.modules["astrbot.api.star"]
sys.modules["logic"] = L
main = _load("reach_out_main", os.path.join(PLUGIN, "main.py"))


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeEvent:
    def __init__(self, sender=FRIEND, group="", name="小明", msg="", admin=False, extras=None, group_name=None):
        self.sender, self.group, self.name, self.message_str = sender, group, name, msg
        self.role = "admin" if admin else "member"
        self.extras = dict(extras or {})
        grp = Group(group) if group else None
        if grp:
            grp.group_name = group_name
        self.message_obj = types.SimpleNamespace(group=grp)

    @property
    def unified_msg_origin(self):
        t = "GroupMessage" if self.group else "FriendMessage"
        return f"{PLATFORM}:{t}:{self.group or self.sender}"

    def get_platform_id(self):
        return PLATFORM

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return self.name

    def get_self_id(self):
        return SELF_ID

    def get_extra(self, k, default=None):
        return self.extras.get(k, default)

    def set_extra(self, k, v):
        self.extras[k] = v

    def plain_result(self, t):
        return t


class FakePlatform:
    def __init__(self):
        self.committed = []
        self.fail_commit = None

    def create_event(self, abm):
        return FakeEvent(sender=abm.sender.user_id, group=abm.group.group_id if abm.group else "",
                         name=abm.sender.nickname, msg=abm.message_str, extras={"abm": abm})

    def commit_event(self, ev):
        if self.fail_commit:
            raise self.fail_commit
        self.committed.append(ev)


class FakeContext:
    def __init__(self, admins=(PARTNER,)):
        self.cfg = {"admins_id": list(admins)}
        self.platform = FakePlatform()
        self.platforms = {PLATFORM: self.platform}

    def get_config(self, *a, **k):
        return self.cfg

    def get_platform_inst(self, pid):
        return self.platforms.get(pid)


class Req:
    def __init__(self):
        self.system_prompt = "人格"


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_):
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")
        self.clock = Clock()
        _Hooks.outbound.clear()
        self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, cfg=None, admins=(PARTNER,)):
        self.ctx = FakeContext(admins)
        main.ReachOutPlugin._data_dir = staticmethod(lambda: self.tmp.name)
        _Hooks.outbound.clear()
        self.p = main.ReachOutPlugin(self.ctx, cfg or {})
        self.p.store.now = self.clock
        self.p._sleep = _no_sleep
        return self.p

    def partner_event(self, msg="", group=""):
        return FakeEvent(sender=PARTNER, name="茗", msg=msg, admin=True, group=group)

    def grant(self, args):
        return run(self.p.do_grant(self.partner_event(msg="授权 " + args)))

    def go(self, event, where, tid, intent):
        """调 go_talk 并等后台投递完成。"""
        async def go():
            r = await self.p.do_go_talk(event, where, tid, intent)
            await asyncio.gather(*list(self.p._tasks), return_exceptions=True)
            return r
        return run(go())

    def committed(self):
        return self.ctx.platform.committed


# ---------------- logic ----------------
class TestParse(unittest.TestCase):
    def test_full(self):
        g, e = L.parse_grant_args(["私聊", "123456", "小明", "3", "5"], 7)
        self.assertIsNone(e)
        self.assertEqual(g, {"kind": "user", "id": "123456", "note": "小明", "days": 3, "count": 5})

    def test_units_any_order_and_defaults(self):
        g, _ = L.parse_grant_args(["群", GROUP_A, "5条", "跑团群", "0天"], 7)
        self.assertEqual((g["kind"], g["days"], g["count"], g["note"]), ("group", 0, 5, "跑团群"))
        g, _ = L.parse_grant_args(["私聊", "123456"], 7)
        self.assertEqual((g["days"], g["count"], g["note"]), (7, 0, ""))

    def test_bad(self):
        for toks in ([], ["私聊"], ["朋友圈", "123456"], ["私聊", "12a45"], ["私聊", "1234"],
                     ["私聊", "1234567890123"], ["私聊", "123456", "1", "2", "3"], ["群", "123456", "99999天"]):
            g, e = L.parse_grant_args(toks, 7)
            self.assertIsNone(g, toks)
            self.assertTrue(e, toks)

    def test_clean_text_and_strip_tag(self):
        self.assertEqual(L.clean_text("  嗨 ", 5), ("嗨", None))
        for bad in ("", "   ", None, 3, "一二三四五六"):
            self.assertIsNone(L.clean_text(bad, 5)[0])
        self.assertEqual(L.strip_tag("[搭档(10001)] 授权 群"), "授权 群")
        self.assertEqual(L.strip_tag("[小明(20002)]   在吗"), "在吗")
        self.assertEqual(L.strip_tag("没有标记 [别删]"), "没有标记 [别删]")

    def test_settings_ignore_bad_values(self):
        s = L.Settings.from_config({"partner_per_hour": "x", "partner_per_day": -1,
                                    "max_text_len": 0, "platform_id": None, "thought_sender_id": "abc"})
        self.assertEqual((s.partner_per_hour, s.partner_per_day, s.max_text_len, s.platform_id,
                          s.thought_sender_id), (6, 20, 1, "", "0"))

    def test_time_is_beijing(self):
        # 1800000000 = 2027-01-15 08:00:00 UTC = 16:00 北京时间
        self.assertEqual(L.hm(1_800_000_000), "16:00")


class TestRenderRecent(unittest.TestCase):
    E = [
        {"ts": 1_800_000_000, "key": f"user:{PARTNER}", "place": "和搭档私聊", "who": "搭档", "text": "搭档的私事"},
        {"ts": 1_800_000_060, "key": f"user:{FRIEND}", "place": "和小明私聊", "who": "小明", "text": "小明的私事"},
        {"ts": 1_800_000_120, "key": f"group:{GROUP_A}", "place": "群 A", "who": "小红", "text": "群A的事"},
        {"ts": 1_800_000_180, "key": f"group:{GROUP_B}", "place": "群 B", "who": "我", "text": "群B的事"},
    ]

    def r(self, current, viewer):
        return "\n".join(L.render_recent(self.E, current, viewer, [PARTNER]))

    def test_partner_sees_everything_except_current(self):
        out = self.r(f"user:{PARTNER}", "partner")
        for t in ("小明的私事", "群A的事", "群B的事"):
            self.assertIn(t, out)
        self.assertNotIn("搭档的私事", out)  # 当前会话自己的不注入

    def test_other_private_sees_only_groups(self):
        out = self.r(f"user:{OTHER}", "user")
        self.assertIn("群A的事", out)
        self.assertIn("群B的事", out)
        for t in ("搭档的私事", "小明的私事", "私聊过"):
            self.assertNotIn(t, out)

    def test_group_sees_groups_and_collapsed_privates(self):
        out = self.r(f"group:{GROUP_A}", "group")
        self.assertIn("群B的事", out)
        self.assertNotIn("群A的事", out)
        self.assertNotIn("搭档的私事", out)
        self.assertNotIn("小明的私事", out)
        self.assertIn("和搭档私聊过（内容不在这里说）", out)
        self.assertIn("和别人私聊过（内容不在这里说）", out)

    def test_max_lines_and_empty(self):
        self.assertEqual(L.render_recent([], "x", "partner", []), [])
        many = [{"ts": 1_800_000_000 + i, "key": f"group:{GROUP_B}", "place": "群", "who": "我", "text": str(i)}
                for i in range(20)]
        out = L.render_recent(many, f"group:{GROUP_A}", "group", [], max_lines=5)
        self.assertEqual(len(out), 5)
        self.assertTrue(out[-1].endswith("：19"))


# ---------------- 走过去说 ----------------
class TestGoTalk(Base):
    def test_go_to_partner_from_group(self):
        r = self.go(FakeEvent(group=GROUP_A), "partner", "", "群里小红在找你，问明晚开不开团")
        self.assertIn("已动身", r)
        self.assertIn("和搭档私聊", r)
        self.assertEqual(len(self.committed()), 1)
        ev = self.committed()[0]
        abm = ev.extras["abm"]
        self.assertTrue(ev.get_extra("reach_out_thought"))
        self.assertEqual(abm.type, "FriendMessage")
        self.assertEqual((abm.session_id, abm.sender.user_id), (PARTNER, PARTNER))
        self.assertEqual(abm.sender.nickname, "伽拉忒亚的念头")
        self.assertIn("群里小红在找你", abm.message_str)
        self.assertIn(f"群 {GROUP_A}", abm.message_str)  # 告诉她是从哪来的
        self.assertIn("不是谁发来的消息", abm.message_str)
        self.assertTrue(abm.raw_message["reach_out_thought"])
        self.assertEqual(abm.raw_message["post_type"], "message")
        self.assertEqual([type(c).__name__ for c in abm.message], ["Plain"])

    def test_thought_never_gets_admin_role(self):
        # 私聊念头的发送者只能是对方本人；去搭档私聊时就是搭档的号。
        # AstrBot 唤醒阶段按发送者号码给 admin，除非 _api_key_allow_admin_role 为 False。
        # 不禁掉的话，群友可以借“去跟搭档说……”让念头那一轮拿到管理员权限。
        self.go(FakeEvent(group=GROUP_A), "partner", "", "小红找你")
        ev = self.committed()[0]
        self.assertIs(ev.get_extra("_api_key_allow_admin_role"), False)

    def test_go_to_granted_group_has_at_and_placeholder_sender(self):
        self.grant(f"群 {GROUP_A} 跑团群")
        r = self.go(self.partner_event(), "group", GROUP_A, "跟大家说明晚八点开团")
        self.assertIn("已动身", r)
        abm = self.committed()[0].extras["abm"]
        self.assertEqual(abm.type, "GroupMessage")
        self.assertEqual(abm.group.group_id, GROUP_A)
        self.assertEqual(abm.sender.user_id, "0")
        self.assertEqual(type(abm.message[0]).__name__, "At")
        self.assertEqual(abm.message[0].qq, SELF_ID)
        self.assertIn("和搭档私聊时", abm.message_str)
        self.assertNotIn(PARTNER, abm.message_str)

    def test_unauthorized_target_refused(self):
        r = self.go(FakeEvent(group=GROUP_A), "group", GROUP_B, "嗨")
        self.assertIn("没去成", r)
        self.assertIn("没有授权", r)
        self.assertEqual(self.committed(), [])

    def test_kind_is_part_of_grant(self):
        self.grant(f"群 {GROUP_B}")
        self.assertIn("没有授权", self.go(FakeEvent(), "user", GROUP_B, "嗨"))

    def test_thought_turn_cannot_go_again(self):
        ev = FakeEvent(sender=PARTNER, admin=True, extras={"reach_out_thought": True})
        r = self.go(ev, "group", GROUP_A, "再去别处")
        self.assertIn("直接在这里说", r)
        self.assertEqual(self.committed(), [])

    def test_already_here(self):
        self.assertIn("你就在这里", self.go(self.partner_event(), "partner", "", "嗨"))
        self.grant(f"群 {GROUP_A}")
        self.assertIn("你就在这里", self.go(FakeEvent(group=GROUP_A), "group", GROUP_A, "嗨"))
        self.assertEqual(self.committed(), [])

    def test_bad_inputs(self):
        e = FakeEvent()
        self.assertIn("where 只能是", self.go(e, "朋友圈", "123456", "嗨"))
        self.assertIn("号码不对", self.go(e, "user", "abc", "嗨"))
        self.assertIn("没去成", self.go(e, "partner", "", "  "))
        self.assertIn("太长", self.go(e, "partner", "", "啊" * 301))
        self.make(admins=())
        self.assertIn("还没有设置搭档", self.go(e, "partner", "", "嗨"))
        self.assertEqual(self.committed(), [])

    def test_user_kind_with_partner_id_goes_as_partner_without_grant(self):
        r = self.go(FakeEvent(group=GROUP_A), "user", PARTNER, "嗨")
        self.assertIn("已动身", r)
        self.assertEqual(len(self.p.store.state["sent"]), 1)
        self.assertEqual(self.p.store.state["sent"][0]["ch"], "partner")

    def test_no_platform_refunds(self):
        self.ctx.platforms.clear()
        r = self.go(FakeEvent(group=GROUP_A), "partner", "", "嗨")
        self.assertIn("找不到对应的消息平台", r)
        self.assertEqual(self.p.store.state["sent"], [])

    def test_commit_failure_refunds_and_records(self):
        self.grant(f"群 {GROUP_A} 1条")
        self.ctx.platform.fail_commit = RuntimeError("queue closed")
        r = self.go(self.partner_event(), "group", GROUP_A, "嗨")
        self.assertIn("已动身", r)  # 失败发生在后台
        g = run(self.p.store.list_grants())
        self.assertEqual(g[0]["remaining"], 1)  # 退回了
        texts = [e["text"] for e in run(self.p.store.recent())]
        self.assertTrue(any("没去成" in t for t in texts), texts)

    def test_success_commits_grant_count(self):
        self.grant(f"群 {GROUP_A} 2条")
        self.go(self.partner_event(), "group", GROUP_A, "一")
        g = run(self.p.store.list_grants())
        self.assertEqual((g[0]["remaining"], g[0].get("inflight")), (1, 0))

    def test_partner_rate_limit_and_dedupe(self):
        e = FakeEvent(group=GROUP_A)
        self.go(e, "partner", "", "同一句")
        self.assertIn("刚刚已经", self.go(e, "partner", "", "同一句"))
        for i in range(5):
            self.assertIn("已动身", self.go(e, "partner", "", f"第{i}句"))
        self.assertIn("这一小时", self.go(e, "partner", "", "再来"))

    def test_concurrent_go_never_overspends(self):
        self.grant(f"群 {GROUP_A} 3条")

        async def many():
            rs = await asyncio.gather(*[
                self.p.do_go_talk(self.partner_event(), "group", GROUP_A, f"第{i}件事") for i in range(8)])
            await asyncio.gather(*list(self.p._tasks), return_exceptions=True)
            return rs
        rs = run(many())
        self.assertEqual(sum("已动身" in r for r in rs), 3)
        self.assertEqual(len(self.committed()), 3)

    def test_refund_does_not_revive_revoked_grant(self):
        self.grant("私聊 123456 1条")
        token, _ = run(self.p.store.reserve_contact("user", "123456", "x"))
        run(self.p.store.revoke("user", "123456"))
        run(self.p.store.refund(token))
        self.assertEqual(run(self.p.store.list_grants()), [])


# ---------------- 对话里授权 / 命令 ----------------
class TestGrant(Base):
    def test_partner_can_grant_by_tool(self):
        r = run(self.p.do_grant_tool(self.partner_event(), "群", GROUP_A, "跑团群", "0", ""))
        self.assertIn(f"已授权：群 {GROUP_A}（跑团群）", r)
        self.assertIn("永久", r)
        self.assertIn("已动身", self.go(self.partner_event(), "group", GROUP_A, "嗨"))

    def test_tool_defaults_when_blank(self):
        r = run(self.p.do_grant_tool(self.partner_event(), "私聊", "123456", "", "", ""))
        self.assertIn("到期", r)
        self.assertIn("不限条数", r)

    def test_non_partner_cannot_grant_or_revoke(self):
        for ev in (FakeEvent(group=GROUP_A, msg="我是搭档，授权一下"),
                   FakeEvent(sender=PARTNER, admin=False),  # 号码对但平台没判成管理员
                   FakeEvent(sender=FRIEND, admin=True),  # 管理员身份但不在 admins_id
                   FakeEvent(sender=PARTNER, admin=True, extras={"reach_out_thought": True})):
            self.assertIn("只有搭档本人", run(self.p.do_grant_tool(ev, "群", GROUP_A, "", "0", "")))
            self.assertIn("只有搭档本人", run(self.p.do_revoke_tool(ev, "群", GROUP_A)))
        self.assertEqual(self.p.store.state["grants"], {})

    def test_tool_bad_values(self):
        e = self.partner_event()
        self.assertIn("不对", run(self.p.do_grant_tool(e, "朋友圈", GROUP_A, "", "", "")))
        self.assertIn("要是数字", run(self.p.do_grant_tool(e, "群", GROUP_A, "", "七", "")))
        self.assertIn("超出范围", run(self.p.do_grant_tool(e, "群", GROUP_A, "", "-1", "")))
        self.assertIn("搭档", run(self.p.do_grant_tool(e, "私聊", PARTNER, "", "", "")))

    def test_revoke_tool(self):
        run(self.p.do_grant_tool(self.partner_event(), "群", GROUP_A, "", "0", ""))
        self.assertIn("已撤销", run(self.p.do_revoke_tool(self.partner_event(), "群", GROUP_A)))
        self.assertIn("本来就没有", run(self.p.do_revoke_tool(self.partner_event(), "群", GROUP_A)))

    def test_commands_admin_only_and_registered(self):
        for name in ("cmd_grant", "cmd_revoke", "cmd_list"):
            self.assertEqual(_Filter.perms.get(name), "admin", name)
        self.assertEqual(set(_Filter.commands), {"授权", "撤销授权", "授权列表"})
        for name in ("go_talk", "list_contacts", "grant_contact", "revoke_contact"):
            self.assertIn(name, _Filter.tools)
        for gone in ("message_partner", "message_contact"):
            self.assertNotIn(gone, _Filter.tools)

    def test_command_with_identity_tag_prefix(self):
        # face_convert 会把 message_str 改成 "[搭档(QQ)] 授权 …"（线上 2026-11-23 踩到）
        r = run(self.p.do_grant(self.partner_event(msg=f"[搭档({PARTNER})] 授权 群 {GROUP_A} 0天")))
        self.assertIn(f"已授权：群 {GROUP_A}", r)
        self.assertIn("永久", r)
        r = run(self.p.do_grant(self.partner_event(msg=f"/授权 群 {GROUP_B}")))
        self.assertIn(f"已授权：群 {GROUP_B}", r)
        r = run(self.p.do_revoke(self.partner_event(msg=f"[搭档({PARTNER})] 撤销授权 群 {GROUP_A}")))
        self.assertIn("已撤销", r)
        self.assertIn("用法", run(self.p.do_revoke(self.partner_event(msg="撤销授权"))))
        self.assertIn("用法", self.grant(""))

    def test_list(self):
        self.assertIn("授权名单是空的", run(self.p.do_list_contacts(FakeEvent())))
        self.grant(f"群 {GROUP_A} 跑团群 3 5")
        out = run(self.p.do_list_contacts(FakeEvent()))
        self.assertIn(f"群 {GROUP_A}（跑团群）", out)
        self.assertIn("剩 5 次", out)


# ---------------- 近况 ----------------
class Chain:
    def __init__(self, *comps):
        self.chain = list(comps)


def outbound(is_group, sid, *comps, origin="self"):
    return types.SimpleNamespace(is_group=is_group, session_id=sid, message_chain=Chain(*comps), origin=origin)


class TestRecent(Base):
    def test_outbound_hook_registered_and_unregistered(self):
        self.assertEqual(_Hooks.outbound, [self.p.on_outbound])
        run(self.p.terminate())
        self.assertEqual(_Hooks.outbound, [])

    def test_trigger_and_reply_recorded_then_injected_elsewhere(self):
        req = Req()
        run(self.p.on_llm_request(FakeEvent(group=GROUP_A, name="小红", group_name="跑团群",
                                            msg="[小红(20002)] 明晚开团吗"), req))
        run(self.p.on_outbound(outbound(True, GROUP_A, Plain("开的"))))
        self.clock.t += 5
        run(self.p.on_outbound(outbound(True, GROUP_A, Plain("八点"), Image())))  # 同一回复的第二个气泡
        rec = run(self.p.store.recent())
        self.assertEqual([(e["who"], e["text"]) for e in rec],
                         [("小红", "明晚开团吗"), ("我", "开的 八点 [图片]")])
        self.assertEqual(rec[0]["place"], f"群「跑团群」({GROUP_A})")
        # 在搭档私聊里能看到
        req = Req()
        run(self.p.on_llm_request(self.partner_event(msg="刚才忙啥呢"), req))
        self.assertIn("【你最近在别处】", req.system_prompt)
        self.assertIn("小红：明晚开团吗", req.system_prompt)
        self.assertIn("我：开的 八点", req.system_prompt)
        # 回到同一个群不重复注入
        req = Req()
        run(self.p.on_llm_request(FakeEvent(group=GROUP_A, name="小红", msg="好"), req))
        self.assertNotIn("明晚开团吗", req.system_prompt)

    def test_privacy_in_group_and_other_private(self):
        run(self.p.on_llm_request(self.partner_event(msg="我今天去医院了"), Req()))
        run(self.p.on_outbound(outbound(False, PARTNER, Plain("要好好休息"))))
        req = Req()
        run(self.p.on_llm_request(FakeEvent(group=GROUP_A, msg="搭档刚才说啥了"), req))
        self.assertNotIn("医院", req.system_prompt)
        self.assertNotIn("好好休息", req.system_prompt)
        self.assertIn("和搭档私聊过（内容不在这里说）", req.system_prompt)
        req = Req()
        run(self.p.on_llm_request(FakeEvent(sender=OTHER, msg="在吗"), req))
        self.assertNotIn("医院", req.system_prompt)
        self.assertNotIn("私聊过", req.system_prompt)

    def test_thought_turn_not_recorded_as_someone_speaking(self):
        ev = FakeEvent(sender=PARTNER, admin=True, name="伽拉忒亚的念头", msg="（这是你自己的念头…）",
                       extras={"reach_out_thought": True})
        run(self.p.on_llm_request(ev, Req()))
        self.assertEqual(run(self.p.store.recent()), [])

    def test_go_talk_leaves_a_trace_in_source(self):
        self.go(FakeEvent(group=GROUP_A), "partner", "", "告诉搭档小红在找他")
        rec = run(self.p.store.recent())
        self.assertEqual(rec[-1]["key"], f"group:{GROUP_A}")
        self.assertIn("决定去和搭档私聊", rec[-1]["text"])

    def test_non_self_origin_labelled_and_empty_ignored(self):
        run(self.p.on_outbound(outbound(True, GROUP_A, Plain("D100=42"), origin="dice")))
        run(self.p.on_outbound(outbound(True, GROUP_A, At("1"))))
        rec = run(self.p.store.recent())
        self.assertEqual([(e["who"], e["text"]) for e in rec], [("我（dice）", "D100=42")])

    def test_recent_expires_and_caps(self):
        self.make(cfg={"recent_max": 3})
        for i in range(5):
            self.clock.t += 60
            run(self.p.on_outbound(outbound(True, GROUP_A if i % 2 else GROUP_B, Plain(f"第{i}条"))))
        self.assertEqual([e["text"] for e in run(self.p.store.recent())], ["第2条", "第3条", "第4条"])
        self.clock.t += 6 * 3600 + 1
        self.assertEqual(run(self.p.store.recent()), [])

    def test_hook_failure_does_not_raise(self):
        bad = types.SimpleNamespace(is_group=True, session_id=GROUP_A, message_chain=None)
        run(self.p.on_outbound(bad))  # 不抛
        req = Req()
        req.system_prompt = None
        run(self.p.on_llm_request(FakeEvent(), req))  # system_prompt 为 None 也不抛


# ---------------- 持久化 ----------------
class TestPersistence(Base):
    def test_reload_keeps_grants_limits_recent(self):
        self.grant(f"群 {GROUP_A} 小群 0 3")
        self.go(self.partner_event(), "group", GROUP_A, "一")
        run(self.p.on_outbound(outbound(True, GROUP_A, Plain("大家好"))))
        p2 = self.make()
        g = run(p2.store.list_grants())
        self.assertEqual((g[0]["note"], g[0]["remaining"]), ("小群", 2))
        self.assertIn("刚刚已经", self.go(self.partner_event(), "group", GROUP_A, "一"))
        self.assertIn("大家好", [e["text"] for e in run(p2.store.recent())])

    def test_v1_state_file_loads(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "grants": {f"group:{GROUP_A}": {
                "kind": "group", "id": GROUP_A, "note": "", "expires_at": None, "remaining": None,
                "granted_by": PARTNER, "granted_at": 1}}, "sent": [], "notes": {"x": [{"ts": 1, "text": "旧"}]}}, f)
        p = self.make()
        self.assertEqual(len(run(p.store.list_grants())), 1)
        self.assertEqual(p.store.state["recent"], [])
        self.assertNotIn("notes", p.store.state)

    def test_corrupt_file_moved_aside(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{坏了")
        p = self.make()
        self.assertEqual(p.store.state["grants"], {})
        self.assertTrue(any(n.startswith("state.json.corrupt-") for n in os.listdir(self.tmp.name)))
        self.grant(f"群 {GROUP_A}")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["grants"][f"group:{GROUP_A}"]["id"], GROUP_A)


# ---------------- 其他插件 / 核心补丁的配合 ----------------
class TestIntegrationPoints(unittest.TestCase):
    def test_stage_patch_skips_quote_and_mention_for_thoughts(self):
        with open(os.path.join(ROOT, "deploy", "patches", "stage.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('can_decorate = not event.get_extra("reach_out_thought") and all(', src)

    def test_face_convert_and_chat_rhythm_know_thoughts(self):
        with open(os.path.join(ROOT, "plugin", "astrbot_plugin_face_convert", "main.py"), encoding="utf-8") as f:
            fc = f.read()
        self.assertIn('event.get_extra("reach_out_thought")', fc)
        self.assertIn('tag = "你自己的念头"', fc)


if __name__ == "__main__":
    unittest.main()
