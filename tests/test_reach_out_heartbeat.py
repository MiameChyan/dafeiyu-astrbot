# -*- coding: utf-8 -*-
"""reach_out 心跳回归：时段、刹车、候选、模型判断、执行（念头）、“没回”暂停、范围、持久化、/心跳。

跑法：python -m unittest tests.test_reach_out_heartbeat（复用 test_reach_out 的空壳；号码全部虚构）。
时钟：1800000000 = 北京时间 16:00（宿主机时区无关，一律按 +8 计算）。
"""
import asyncio
import json
import sys
import types
import unittest

T = sys.modules.get("test_reach_out") or sys.modules.get("tests.test_reach_out")
if T is None:
    try:
        from tests import test_reach_out as T
    except ImportError:
        import test_reach_out as T

L, M, HB, run = T.L, T.MIND, T.HB, T.run
PARTNER, FRIEND, GROUP_A = T.PARTNER, T.FRIEND, T.GROUP_A
H = 3600


class FakeProvider:
    def __init__(self, reply=None, exc=None, delay=0):
        self.reply, self.exc, self.delay = reply, exc, delay
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, **k):
        self.calls.append((prompt, system_prompt))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)
        return types.SimpleNamespace(completion_text=text)


def partner_ev(**k):
    return T.FakeEvent(sender=PARTNER, name="茗", admin=True, **k)


class Base(T.Base):
    def setUp(self):
        super().setUp()
        self.prov = FakeProvider()
        self.ctx.get_provider_by_id = lambda pid: self.prov

    def make(self, cfg=None, admins=(PARTNER,)):
        p = super().make(cfg, admins)
        if hasattr(self, "prov"):
            self.ctx.get_provider_by_id = lambda pid: self.prov
        return p

    def seed(self, text="搭档说最近在赶论文", kind="牵挂", about="partner", hours_ago=5):
        """记一件关于搭档的心事，并让平台信息被记住；然后把时间往后拨。"""
        self.clock.t -= hours_ago * H
        run(self.p.on_llm_request(T.FakeEvent(sender=FRIEND, group=GROUP_A, msg="随便聊聊"), T.Req()))
        r = run(self.p.do_keep_in_mind(partner_ev(), kind, about, text, ""))
        self.assertIn("记在心上了", r)
        self.clock.t += hours_ago * H
        return [i for i in run(self.p.mind.items()) if i["text"] == text][0]

    def act_reply(self, item, intent="想问问论文赶得怎么样了"):
        self.prov.reply = {"act": True, "item_id": item["id"], "intent": intent, "reason": "下午有空，正合适"}

    def tick(self):
        async def go():
            r = await self.p.heartbeat_tick()
            await asyncio.gather(*list(self.p._tasks), return_exceptions=True)
            return r
        return run(go())

    def records(self):
        return run(self.p.beat.snapshot())["records"]


class TestPure(unittest.TestCase):
    def test_window_and_slots(self):
        w = HB.parse_window("09:30-23:30")
        self.assertEqual(w, (570, 1410))
        self.assertEqual(HB.parse_window("乱写"), (570, 1410))
        self.assertEqual(HB.parse_window("23:00-01:00"), (570, 1410))
        base = 1_800_000_000  # 16:00
        self.assertEqual(HB.slot_of(base, w, 45)[1], (960 - 570) // 45)
        self.assertIsNone(HB.slot_of(base - 6.5 * H - 60, w, 45))  # 09:29
        self.assertEqual(HB.slot_of(base - 6.5 * H, w, 45)[1], 0)  # 09:30
        self.assertIsNotNone(HB.slot_of(base + 7.5 * H - 60, w, 45))  # 23:29
        self.assertIsNone(HB.slot_of(base + 7.5 * H, w, 45))  # 23:30

    def test_candidates(self):
        now = 1_800_000_000
        key = f"user:{PARTNER}"
        base = {"status": "active", "about": key, "origin": key, "kind": "牵挂", "surfaced": 0,
                "created_at": now - 5 * H, "due_at": None}
        items = [
            dict(base, id="a"),  # 挂了 5 小时、没提过 → 可以
            dict(base, id="b", created_at=now - 1 * H),  # 太新
            dict(base, id="c", kind="想分享", created_at=now - 60),  # 想分享随时可以
            dict(base, id="d", surfaced=1),  # 提过了
            dict(base, id="e", surfaced=3, due_at=now - 10),  # 到期 → 可以，排最前
            dict(base, id="f", hb_tried_at=now - 2 * H),  # 心跳 24 小时内试过
            dict(base, id="g", about=f"user:{FRIEND}"),  # 不是这个对象
            dict(base, id="h", status="done"),
            dict(base, id="i", kind="想分享", surfaced=1),  # 想分享但已注入过
        ]
        got = [i["id"] for i in HB.candidates(items, key, "partner", [PARTNER], now)]
        self.assertEqual(got, ["e", "a", "c"])
        # 对其他人：只看对方私聊里记的 + 群里记的关于他的
        fk = f"user:{FRIEND}"
        items2 = [dict(base, id="x", about=fk, origin=f"group:{GROUP_A}"),
                  dict(base, id="y", about=fk, origin=f"user:{PARTNER}")]
        self.assertEqual([i["id"] for i in HB.candidates(items2, fk, "user", [PARTNER], now)], ["x"])

    def test_parse_judge(self):
        ids = {"m1"}
        ok = '好：{"act": true, "item_id": "m1", "intent": "问问论文", "reason": "有空"}'
        self.assertEqual(HB.parse_judge(ok, ids, 300)[0]["intent"], "问问论文")
        self.assertFalse(HB.parse_judge('{"act": false, "reason": "在忙"}', ids, 300)[0]["act"])
        self.assertFalse(HB.parse_judge('{"act": "yes", "item_id": "m1", "intent": "x"}', ids, 300)[0]["act"])
        for bad in ("不知道", "{坏", '{"act": true, "item_id": "m9", "intent": "x"}',
                    '{"act": true, "item_id": "m1", "intent": ""}', "[1,2]"):
            d, err = HB.parse_judge(bad, ids, 300)
            self.assertIsNone(d, bad)
            self.assertTrue(err, bad)

    def test_judge_prompt_mentions_time_and_items(self):
        now = 1_800_000_000
        it = {"id": "m1", "kind": "牵挂", "text": "论文", "created_at": now - 5 * H, "due_at": None, "surfaced": 0}
        p = HB.judge_prompt(now, "搭档", 1, 2, [it], ["- 15:00 群 A 小明：hi"])
        for s in ("16:00", "星期", "主动找过人 1 次（上限 2）", "[m1] 牵挂：论文", "小明：hi"):
            self.assertIn(s, p)
        self.assertIn("大多数时候答案是不去", HB.JUDGE_SYSTEM)


class TestTick(Base):
    def test_happy_path_goes_to_partner(self):
        item = self.seed()
        self.act_reply(item)
        self.assertEqual(self.tick(), "去了")
        self.assertEqual(len(self.committed()), 1)
        abm = self.committed()[0].extras["abm"]
        self.assertEqual((abm.type, abm.session_id), ("FriendMessage", PARTNER))
        self.assertIn("一个人待着的时候想起来", abm.message_str)
        self.assertIn("想问问论文赶得怎么样了", abm.message_str)
        self.assertIs(self.committed()[0].get_extra("_api_key_allow_admin_role"), False)
        st = run(self.p.beat.snapshot())
        self.assertEqual(st["count"], 1)
        self.assertEqual(st["ignore"][f"user:{PARTNER}"]["pending_since"], self.clock())
        it = run(self.p.mind.items())[0]
        self.assertEqual((it["surfaced"], it["hb_tried_at"]), (1, self.clock()))
        self.assertEqual(self.records()[-1]["result"], "去了")
        self.assertIn("论文", self.prov.calls[0][0])

    def test_outside_window_and_same_slot(self):
        self.seed()
        self.clock.t -= 8 * H  # 08:00
        self.assertIsNone(self.tick())
        self.clock.t += 8 * H
        self.prov.reply = {"act": False, "reason": "在忙"}
        self.assertEqual(self.tick(), "不动")
        self.assertIsNone(self.tick())  # 同一时段只跑一次
        self.clock.t += 45 * 60
        self.assertEqual(self.tick(), "不动")
        self.assertEqual(len(self.prov.calls), 2)

    def test_model_says_no_or_fails(self):
        item = self.seed()
        cases = [{"act": False, "reason": "刚聊过不久"}, "乱码", {"act": True, "item_id": "m00000", "intent": "x"}]
        for reply in cases:
            self.prov.reply = reply
            self.clock.t += 45 * 60
            self.assertEqual(self.tick(), "不动", reply)
        self.prov.exc = RuntimeError("x")
        self.clock.t += 45 * 60
        self.assertEqual(self.tick(), "不动")
        self.prov.exc, self.prov.delay = None, 0.05
        self.make(cfg={"heartbeat_timeout_seconds": 0})
        self.clock.t += 45 * 60
        self.assertEqual(self.tick(), "不动")
        self.assertEqual(self.committed(), [])
        reasons = [r["reason"] for r in self.records()]
        self.assertTrue(any("刚聊过不久" in r for r in reasons), reasons)
        self.assertTrue(any("不在候选里" in r for r in reasons), reasons)
        self.assertTrue(any("模型出错" in r for r in reasons), reasons)
        self.assertTrue(any("超时" in r for r in reasons), reasons)
        self.assertEqual(item["surfaced"], 0)

    def test_no_provider(self):
        self.seed()
        self.ctx.get_provider_by_id = lambda pid: None
        self.assertEqual(self.tick(), "不动")
        self.assertIn("找不到模型提供商", self.records()[-1]["reason"])

    def test_no_candidates_no_model_call(self):
        run(self.p.on_llm_request(T.FakeEvent(group=GROUP_A, msg="hi"), T.Req()))
        self.assertEqual(self.tick(), "不动")
        self.assertIn("没有可说的事", self.records()[-1]["reason"])
        self.assertEqual(self.prov.calls, [])

    def test_unknown_platform(self):
        run(self.p.do_keep_in_mind(partner_ev(), "想分享", "partner", "看到一只猫", ""))
        self.assertEqual(self.tick(), "不动")
        self.assertIn("还不知道往哪发", self.records()[-1]["reason"])

    def test_busy_brake(self):
        self.seed()
        # 搭档刚在群里说过话（在搭档私聊里说的话会注入这件心事、算提起过，就不是心跳要测的路径了）
        run(self.p.on_llm_request(partner_ev(msg="大家好", group=GROUP_A), T.Req()))
        self.assertEqual(self.tick(), "不动")
        self.assertIn("刚和对方聊过", self.records()[-1]["reason"])
        self.clock.t += 31 * 60
        self.act_reply(run(self.p.mind.items())[0])
        self.assertEqual(self.tick(), "去了")

    def test_daily_max_and_gap(self):
        a = self.seed(text="论文的事")
        self.act_reply(a)
        self.assertEqual(self.tick(), "去了")
        self.clock.t += 45 * 60
        self.assertEqual(self.tick(), "不动")
        self.assertIn("不到 3 小时", self.records()[-1]["reason"])
        self.assertEqual(self.prov.calls.__len__(), 1)
        # 今天的第二次
        b = run(self.p.do_keep_in_mind(partner_ev(), "想分享", "partner", "看到一只很胖的猫", ""))
        self.assertIn("记在心上了", b)
        b = [i for i in run(self.p.mind.items()) if "猫" in i["text"]][0]
        self.clock.t += 3 * H
        self.act_reply(b, "给搭档讲那只猫")
        self.assertEqual(self.tick(), "去了")
        self.clock.t += 3 * H  # 22:45，还在窗口里，但今天已满 2 次
        self.assertEqual(self.tick(), "不动")
        self.assertIn("今天已经主动找过 2 次", self.records()[-1]["reason"])
        self.clock.t += 45 * 60  # 23:30 以后不在窗口
        self.assertIsNone(self.tick())
        self.assertEqual(run(self.p.beat.snapshot())["count"], 2)

    def test_daily_cap_blocks_third(self):
        self.make(cfg={"heartbeat_daily_max": 1})
        a = self.seed()
        self.act_reply(a)
        self.assertEqual(self.tick(), "去了")
        self.clock.t += 4 * H
        self.assertEqual(self.tick(), "不动")
        self.assertIn("今天已经主动找过 1 次", self.records()[-1]["reason"])

    def test_ignored_twice_pauses_a_day(self):
        self.make(cfg={"heartbeat_daily_max": 10, "heartbeat_min_gap_hours": 0, "heartbeat_window": "00:00-24:00",
                       "heartbeat_interval_minutes": 60})
        for n in range(2):
            text = ["楼下开了家烤红薯店", "看到一只很胖的橘猫"][n]
            run(self.p.do_keep_in_mind(partner_ev(), "想分享", "partner", text, ""))
            self.clock.t += 1
            run(self.p.on_llm_request(T.FakeEvent(group=GROUP_A, msg="hi"), T.Req()))
            it = [i for i in run(self.p.mind.items()) if i["text"] == text][0]
            self.act_reply(it, f"说第{n}件")
            self.clock.t += 31 * 60
            self.assertEqual(self.tick(), "去了")
            self.clock.t += 6 * H + 1  # 搭档 6 小时没说话
        run(self.p.do_keep_in_mind(partner_ev(), "想分享", "partner", "周末天气预报说要下雪", ""))
        self.clock.t += 60 * 60
        self.assertEqual(self.tick(), "不动")
        self.assertIn("先停一天", self.records()[-1]["reason"])
        st = run(self.p.beat.snapshot())
        self.assertGreater(st["ignore"][f"user:{PARTNER}"]["paused_until"], self.clock())

    def test_reply_clears_ignore(self):
        a = self.seed()
        self.act_reply(a)
        self.tick()
        self.clock.t += 60 * 60
        run(self.p.on_llm_request(partner_ev(msg="论文还行"), T.Req()))  # 搭档回了
        self.clock.t += 6 * H  # 回复那条近况已过期（保留 6 小时），也不能算“没回”
        self.tick()
        ig = run(self.p.beat.snapshot())["ignore"][f"user:{PARTNER}"]
        self.assertEqual((ig["count"], ig["pending_since"]), (0, 0))

    def test_reply_in_group_also_counts(self):
        a = self.seed()
        self.act_reply(a)
        self.tick()
        run(self.p.on_llm_request(partner_ev(msg="我在群里", group=GROUP_A), T.Req()))
        ig = run(self.p.beat.snapshot())["ignore"][f"user:{PARTNER}"]
        self.assertEqual(ig["pending_since"], 0)

    def test_scope(self):
        run(self.p.do_grant_tool(partner_ev(), "私聊", FRIEND, "小明", "0", ""))
        run(self.p.do_grant_tool(partner_ev(), "群", GROUP_A, "", "0", ""))
        targets = [k for k, _, _ in self.p._heartbeat_targets(run(self.p.store.list_grants()))]
        self.assertEqual(targets, [f"user:{PARTNER}"])
        self.make(cfg={"heartbeat_scope": "partner+granted"})
        targets = [k for k, _, _ in self.p._heartbeat_targets(run(self.p.store.list_grants()))]
        self.assertEqual(targets, [f"user:{PARTNER}", f"user:{FRIEND}"])  # 群永远不进心跳

    def test_granted_friend_when_partner_has_nothing(self):
        self.make(cfg={"heartbeat_scope": "partner+granted"})
        run(self.p.do_grant_tool(partner_ev(), "私聊", FRIEND, "小明", "0", ""))
        self.clock.t -= 5 * H
        run(self.p.on_llm_request(T.FakeEvent(sender=FRIEND, group=GROUP_A, msg="明天考试"), T.Req()))
        run(self.p.do_keep_in_mind(T.FakeEvent(sender=FRIEND, group=GROUP_A, name="小明"), "牵挂", "here", "小明明天考试", ""))
        self.clock.t += 5 * H
        it = run(self.p.mind.items())[0]
        self.act_reply(it, "问小明考得怎么样")
        self.assertEqual(self.tick(), "去了")
        abm = self.committed()[0].extras["abm"]
        self.assertEqual(abm.session_id, FRIEND)
        self.assertEqual(run(self.p.store.list_grants())[0]["kind"], "user")

    def test_commit_failure_not_counted(self):
        a = self.seed()
        self.act_reply(a)
        self.ctx.platform.fail_commit = RuntimeError("closed")
        self.assertEqual(self.tick(), "不动")
        self.assertIn("没送出去", self.records()[-1]["reason"])
        self.assertEqual(run(self.p.beat.snapshot())["count"], 0)
        self.assertEqual(run(self.p.store.recent())[-1]["text"], "（想去和搭档私聊，但没去成）")

    def test_persistence_and_status(self):
        a = self.seed()
        self.act_reply(a)
        self.tick()
        p2 = self.make()
        st = run(p2.beat.snapshot())
        self.assertEqual((st["count"], st["self_id"]), (1, T.SELF_ID))
        out = run(p2.do_heartbeat_status())
        self.assertIn("今天主动 1/2 次", out)
        self.assertIn("去了 → 搭档", out)
        self.assertIn(a["id"], out)

    def test_loop_starts_only_when_enabled(self):
        run(self.p.on_llm_request(partner_ev(msg="hi"), T.Req()))
        self.assertIsNone(self.p._heart)
        self.make(cfg={"heartbeat_enabled": True})

        async def go():
            await self.p.on_llm_request(partner_ev(msg="hi"), T.Req())
            started = self.p._heart is not None
            await self.p.terminate()
            return started
        self.assertTrue(run(go()))

    def test_corrupt_state(self):
        import os
        with open(os.path.join(self.tmp.name, "heartbeat.json"), "w", encoding="utf-8") as f:
            f.write("{坏")
        p = self.make()
        self.assertEqual(run(p.beat.snapshot())["count"], 0)
        self.assertTrue(any(n.startswith("heartbeat.json.corrupt-") for n in os.listdir(self.tmp.name)))


if __name__ == "__main__":
    unittest.main()
