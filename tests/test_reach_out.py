# -*- coding: utf-8 -*-
"""reach_out 插件回归：私信搭档、白名单授权、限流、持久化。

跑法（纯标准库，不需要 AstrBot、不联网）：python -m unittest tests.test_reach_out
原理：logic.py 直接测；插件入口用空壳 astrbot 模块 + 假 Context（记录 send_message）。
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
PLATFORM = "qq"


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


class _Chain:
    def __init__(self):
        self.text = ""

    def message(self, t):
        self.text += t
        return self


class _Logger:
    def __init__(self):
        self.lines = []

    def info(self, m, *a):
        self.lines.append(str(m))

    debug = warning = error = info


LOG = _Logger()
for pkg in ("astrbot", "astrbot.api"):
    _stub(pkg)
_stub("astrbot.api", logger=LOG)
_stub("astrbot.api.event", AstrMessageEvent=object, MessageChain=_Chain, filter=_Filter)
_stub("astrbot.api.event.filter", PermissionType=_PermissionType, permission_type=_permission_type)
_stub("astrbot.api.star", Context=object, Star=_Star, register=lambda *a, **k: (lambda c: c))
sys.modules["astrbot.api"].event = sys.modules["astrbot.api.event"]
sys.modules["astrbot.api"].star = sys.modules["astrbot.api.star"]
sys.modules["logic"] = L
main = _load("reach_out_main", os.path.join(PLUGIN, "main.py"))


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeContext:
    def __init__(self, admins=(PARTNER,)):
        self.cfg = {"admins_id": list(admins)}
        self.sent = []
        self.fail = {}  # umo -> 异常 或 False

    def get_config(self, *a, **k):
        return self.cfg

    async def send_message(self, umo, chain):
        f = self.fail.get(umo)
        if isinstance(f, Exception):
            raise f
        if f is False:
            return False
        self.sent.append((umo, chain.text))
        return True


class FakeEvent:
    def __init__(self, sender="20002", group="", name="小明", msg="", admin=False):
        self.sender, self.group, self.name, self.message_str = sender, group, name, msg
        self.role = "admin" if admin else "member"

    @property
    def unified_msg_origin(self):
        if self.group:
            return f"{PLATFORM}:GroupMessage:{self.group}"
        return f"{PLATFORM}:FriendMessage:{self.sender}"

    def get_platform_id(self):
        return PLATFORM

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return self.name

    def plain_result(self, t):
        return t


class Req:
    def __init__(self):
        self.system_prompt = "人格"


def run(coro):
    return asyncio.run(coro)


def umo(kind, tid):
    return L.build_umo(PLATFORM, kind, tid)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")
        self.clock = Clock()
        self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, cfg=None, admins=(PARTNER,)):
        self.ctx = FakeContext(admins)
        main.ReachOutPlugin._data_dir = staticmethod(lambda: self.tmp.name)
        self.p = main.ReachOutPlugin(self.ctx, cfg or {})
        self.p.store.now = self.clock
        return self.p

    def grant(self, args):
        return run(self.p.do_grant(FakeEvent(sender=PARTNER, msg="授权 " + args, admin=True)))


class TestParse(unittest.TestCase):
    def test_full(self):
        g, e = L.parse_grant_args(["私聊", "123456", "小明", "3", "5"], 7)
        self.assertIsNone(e)
        self.assertEqual(g, {"kind": "user", "id": "123456", "note": "小明", "days": 3, "count": 5})

    def test_units_any_order_and_defaults(self):
        g, _ = L.parse_grant_args(["群", "987654321", "5条", "跑团群", "0天"], 7)
        self.assertEqual((g["kind"], g["days"], g["count"], g["note"]), ("group", 0, 5, "跑团群"))
        g, _ = L.parse_grant_args(["私聊", "123456"], 7)
        self.assertEqual((g["days"], g["count"], g["note"]), (7, 0, ""))

    def test_bad(self):
        for toks in ([], ["私聊"], ["朋友圈", "123456"], ["私聊", "12a45"], ["私聊", "1234"],
                     ["私聊", "1234567890123"], ["私聊", "123456", "1", "2", "3"], ["群", "123456", "99999天"]):
            g, e = L.parse_grant_args(toks, 7)
            self.assertIsNone(g, toks)
            self.assertTrue(e, toks)

    def test_clean_text(self):
        self.assertEqual(L.clean_text("  嗨 ", 5), ("嗨", None))
        for bad in ("", "   ", None, 3, "一二三四五六"):
            self.assertIsNone(L.clean_text(bad, 5)[0])

    def test_settings_ignore_bad_values(self):
        s = L.Settings.from_config({"partner_per_hour": "x", "partner_per_day": -1, "cc_partner": False,
                                    "max_text_len": 0, "platform_id": None})
        self.assertEqual((s.partner_per_hour, s.partner_per_day, s.cc_partner, s.max_text_len, s.platform_id),
                         (6, 20, False, 1, ""))


class TestPartner(Base):
    def test_from_group_happy(self):
        r = run(self.p.do_message_partner(FakeEvent(group="555666"), "群里有人找你"))
        self.assertIn("已私信搭档", r)
        self.assertEqual(self.ctx.sent, [(umo("user", PARTNER), "群里有人找你")])
        # 搭档会话里会提示她刚主动发过什么、在哪发的
        req = Req()
        run(self.p.inject_recent_outbound(FakeEvent(sender=PARTNER, admin=True), req))
        self.assertIn("群 555666", req.system_prompt)
        self.assertIn("群里有人找你", req.system_prompt)
        self.assertTrue(req.system_prompt.startswith("人格"))

    def test_already_in_partner_private_chat(self):
        r = run(self.p.do_message_partner(FakeEvent(sender=PARTNER, admin=True), "嗨"))
        self.assertIn("直接回复", r)
        self.assertEqual(self.ctx.sent, [])

    def test_no_partner(self):
        self.make(admins=())
        r = run(self.p.do_message_partner(FakeEvent(), "嗨"))
        self.assertIn("没发出去", r)

    def test_non_numeric_admin_ids_ignored(self):
        self.make(admins=("astrbot", " 10001 ", "10001"))
        run(self.p.do_message_partner(FakeEvent(), "嗨"))
        self.assertEqual(self.ctx.sent, [(umo("user", PARTNER), "嗨")])

    def test_empty_and_too_long(self):
        self.assertIn("没发出去", run(self.p.do_message_partner(FakeEvent(), "  ")))
        self.assertIn("太长", run(self.p.do_message_partner(FakeEvent(), "啊" * 501)))
        self.assertEqual(self.ctx.sent, [])

    def test_send_failure_refunds_and_says_not_sent(self):
        self.ctx.fail[umo("user", PARTNER)] = RuntimeError("ActionFailed")
        r = run(self.p.do_message_partner(FakeEvent(), "第一句"))
        self.assertIn("没发出去", r)
        self.assertIn("不要说已经发了", r)
        self.assertEqual(self.p.store.state["sent"], [])
        self.assertEqual(self.p.store.state["notes"], {})
        self.ctx.fail.clear()
        self.assertIn("已私信", run(self.p.do_message_partner(FakeEvent(), "第一句")))

    def test_platform_not_found(self):
        self.ctx.fail[umo("user", PARTNER)] = False
        self.assertIn("找不到对应的消息平台", run(self.p.do_message_partner(FakeEvent(), "嗨")))

    def test_dedupe_then_allowed_after_window(self):
        e = FakeEvent()
        run(self.p.do_message_partner(e, "同一句"))
        self.assertIn("刚刚已经发给搭档", run(self.p.do_message_partner(e, "同一句")))
        self.clock.t += 601
        self.assertIn("已私信", run(self.p.do_message_partner(e, "同一句")))

    def test_hour_and_day_limits(self):
        e = FakeEvent()
        for i in range(6):
            self.assertIn("已私信", run(self.p.do_message_partner(e, f"第{i}句")))
        self.assertIn("这一小时", run(self.p.do_message_partner(e, "第7句")))
        for h in range(1, 4):
            self.clock.t += 3601
            for i in range(6):
                run(self.p.do_message_partner(e, f"{h}-{i}"))
        self.assertEqual(len(self.ctx.sent), 20)
        self.clock.t += 3601
        self.assertIn("今天", run(self.p.do_message_partner(e, "再来")))
        self.clock.t += 86400
        self.assertIn("已私信", run(self.p.do_message_partner(e, "第二天")))

    def test_two_partners_one_fails(self):
        self.make(admins=(PARTNER, "10002"))
        self.ctx.fail[umo("user", "10002")] = RuntimeError("not friend")
        r = run(self.p.do_message_partner(FakeEvent(), "嗨"))
        self.assertIn("已私信", r)
        self.assertIn("10002", r)
        self.assertEqual(len(self.p.store.state["sent"]), 1)


class TestGrantCommands(Base):
    def test_admin_only_decorated(self):
        for name in ("cmd_grant", "cmd_revoke", "cmd_list"):
            self.assertEqual(_Filter.perms.get(name), "admin", name)
        self.assertEqual(set(_Filter.commands), {"授权", "撤销授权", "授权列表"})

    def test_grant_list_revoke(self):
        r = self.grant("私聊 123456 小明 3 5")
        self.assertIn("已授权", r)
        self.assertIn("5 条", r)
        self.assertIn("QQ 好友", r)
        lst = run(self.p.do_list_contacts(FakeEvent()))
        self.assertIn("123456（小明）", lst)
        self.assertIn("剩 5 条", lst)
        r = run(self.p.do_revoke(FakeEvent(sender=PARTNER, msg="撤销授权 私聊 123456", admin=True)))
        self.assertIn("已撤销", r)
        self.assertIn("本来就没有", run(self.p.do_revoke(FakeEvent(msg="撤销授权 私聊 123456"))))
        self.assertIn("用法", run(self.p.do_revoke(FakeEvent(msg="撤销授权"))))

    def test_grant_with_identity_tag_prefix(self):
        # face_convert 会把 message_str 改成 "[搭档(QQ)] 授权 …"（线上 2026-11-23 实际踩到）
        r = run(self.p.do_grant(FakeEvent(sender=PARTNER, admin=True,
                                          msg=f"[搭档({PARTNER})] 授权 群 630569324 0天")))
        self.assertIn("已授权：群 630569324", r)
        self.assertIn("永久", r)
        r = run(self.p.do_grant(FakeEvent(sender=PARTNER, admin=True, msg="/授权 群 630569325")))
        self.assertIn("已授权：群 630569325", r)
        r = run(self.p.do_revoke(FakeEvent(sender=PARTNER, admin=True,
                                           msg=f"[搭档({PARTNER})] 撤销授权 群 630569324")))
        self.assertIn("已撤销", r)

    def test_grant_partner_refused_and_bad_usage(self):
        self.assertIn("搭档", self.grant(f"私聊 {PARTNER}"))
        self.assertIn("用法", self.grant(""))
        self.assertEqual(self.p.store.state["grants"], {})

    def test_regrant_replaces(self):
        self.grant("群 555666 2条")
        self.grant("群 555666 0天")
        g = run(self.p.store.list_grants())
        self.assertEqual(len(g), 1)
        self.assertIsNone(g[0]["expires_at"])
        self.assertIsNone(g[0]["remaining"])


class TestContact(Base):
    def test_unauthorized(self):
        r = run(self.p.do_message_contact(FakeEvent(), "user", "123456", "嗨"))
        self.assertIn("没有授权", r)
        self.assertEqual(self.ctx.sent, [])

    def test_happy_private_with_cc_and_note(self):
        self.grant("私聊 123456 小红 7 2")
        r = run(self.p.do_message_contact(FakeEvent(group="555666"), "user", "123456", "明晚跑团别忘了"))
        self.assertIn("已发给私聊 123456（小红）", r)
        self.assertIn("还剩 1 条", r)
        self.assertEqual(self.ctx.sent[0], (umo("user", "123456"), "明晚跑团别忘了"))
        self.assertEqual(self.ctx.sent[1][0], umo("user", PARTNER))
        self.assertIn("抄送", self.ctx.sent[1][1])
        req = Req()
        run(self.p.inject_recent_outbound(FakeEvent(sender="123456"), req))
        self.assertIn("明晚跑团别忘了", req.system_prompt)

    def test_group_kind_aliases(self):
        self.grant("群 555666")
        r = run(self.p.do_message_contact(FakeEvent(), "群", "555666", "大家好"))
        self.assertIn("已发给群 555666", r)
        self.assertEqual(self.ctx.sent[0][0], umo("group", "555666"))

    def test_kind_is_part_of_grant(self):
        self.grant("群 555666")
        self.assertIn("没有授权", run(self.p.do_message_contact(FakeEvent(), "user", "555666", "嗨")))

    def test_no_cc_when_disabled_or_partner_watching(self):
        self.make(cfg={"cc_partner": False})
        self.grant("私聊 123456")
        run(self.p.do_message_contact(FakeEvent(), "user", "123456", "嗨"))
        self.assertEqual(len(self.ctx.sent), 1)
        self.make()
        run(self.p.do_message_contact(FakeEvent(sender=PARTNER, admin=True), "user", "123456", "嗨2"))
        self.assertEqual(len(self.ctx.sent), 1)

    def test_count_exhausted_and_expiry(self):
        self.grant("私聊 123456 1条")
        self.assertIn("已发给", run(self.p.do_message_contact(FakeEvent(), "user", "123456", "一")))
        self.assertIn("没有授权", run(self.p.do_message_contact(FakeEvent(), "user", "123456", "二")))
        self.grant("私聊 123456 1天")
        self.clock.t += 86401
        self.assertIn("没有授权", run(self.p.do_message_contact(FakeEvent(), "user", "123456", "三")))
        self.assertEqual(run(self.p.store.list_grants()), [])

    def test_failure_refunds_count_even_last_one(self):
        self.grant("私聊 123456 1条")
        self.ctx.fail[umo("user", "123456")] = RuntimeError("not friend")
        r = run(self.p.do_message_contact(FakeEvent(), "user", "123456", "一"))
        self.assertIn("没发出去", r)
        self.assertEqual(len(self.ctx.sent), 0)  # 不抄送
        g = run(self.p.store.list_grants())
        self.assertEqual(g[0]["remaining"], 1)
        self.ctx.fail.clear()
        self.assertIn("已发给", run(self.p.do_message_contact(FakeEvent(), "user", "123456", "一")))

    def test_refund_does_not_revive_revoked_grant(self):
        self.grant("私聊 123456 1条")
        token, _ = run(self.p.store.reserve_contact("user", "123456", "x"))
        run(self.p.store.revoke("user", "123456"))
        run(self.p.store.refund(token))
        self.assertEqual(run(self.p.store.list_grants()), [])

    def test_per_target_and_hourly_limits(self):
        self.make(cfg={"contact_per_target_day": 2, "contact_per_hour": 3})
        self.grant("私聊 123456")
        self.grant("私聊 654321")
        e = FakeEvent()
        run(self.p.do_message_contact(e, "user", "123456", "1"))
        run(self.p.do_message_contact(e, "user", "123456", "2"))
        self.assertIn("今天已经给", run(self.p.do_message_contact(e, "user", "123456", "3")))
        run(self.p.do_message_contact(e, "user", "654321", "1"))
        self.assertIn("这一小时", run(self.p.do_message_contact(e, "user", "654321", "2")))

    def test_bad_inputs_and_self_session(self):
        e = FakeEvent()
        self.assertIn("kind", run(self.p.do_message_contact(e, "朋友圈", "123456", "嗨")))
        self.assertIn("号码不对", run(self.p.do_message_contact(e, "user", "abc", "嗨")))
        self.assertIn("message_partner", run(self.p.do_message_contact(e, "user", PARTNER, "嗨")))
        self.grant("私聊 20002")
        self.assertIn("直接回复", run(self.p.do_message_contact(e, "user", "20002", "嗨")))
        self.assertEqual(self.ctx.sent, [])

    def test_concurrent_reserve_never_overspends(self):
        self.grant("私聊 123456 3条")

        async def many():
            return await asyncio.gather(*[
                self.p.do_message_contact(FakeEvent(), "user", "123456", f"第{i}条") for i in range(8)
            ])
        rs = run(many())
        self.assertEqual(sum("已发给" in r for r in rs), 3)
        self.assertEqual(sum(1 for u, _ in self.ctx.sent if u == umo("user", "123456")), 3)


class TestPersistence(Base):
    def test_reload_keeps_grants_limits_notes(self):
        self.grant("私聊 123456 小红 0 3")
        run(self.p.do_message_contact(FakeEvent(), "user", "123456", "一"))
        p2 = self.make()
        g = run(p2.store.list_grants())
        self.assertEqual((g[0]["note"], g[0]["remaining"]), ("小红", 2))
        self.assertIn("刚刚已经发过", run(p2.do_message_contact(FakeEvent(), "user", "123456", "一")))
        req = Req()
        run(p2.inject_recent_outbound(FakeEvent(sender="123456"), req))
        self.assertIn("一", req.system_prompt)

    def test_corrupt_file_moved_aside(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{坏了")
        p = self.make()
        self.assertEqual(p.store.state["grants"], {})
        self.assertTrue(any(n.startswith("state.json.corrupt-") for n in os.listdir(self.tmp.name)))
        self.grant("群 555666")
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("555666", json.load(f)["grants"]["group:555666"]["id"])

    def test_notes_expire_and_cap(self):
        for i in range(7):
            run(self.p.store.add_note("s", f"n{i}"))
        self.assertEqual([n["text"] for n in run(self.p.store.notes_for("s"))], ["n2", "n3", "n4", "n5", "n6"])
        self.clock.t += 72 * 3600 + 1
        self.assertEqual(run(self.p.store.notes_for("s")), [])
        req = Req()
        run(self.p.inject_recent_outbound(FakeEvent(), req))
        self.assertEqual(req.system_prompt, "人格")


class TestToolsRegistered(unittest.TestCase):
    def test_tool_names(self):
        for name in ("message_partner", "message_contact", "list_contacts"):
            self.assertIn(name, _Filter.tools)


if __name__ == "__main__":
    unittest.main()
