# -*- coding: utf-8 -*-
"""reach_out 心事账本回归：keep_in_mind / let_go、注入与防唠叨、隐私矩阵、上限、补漏、持久化。

跑法：python -m unittest tests.test_reach_out_mind（复用 test_reach_out 的空壳和假对象；号码全部虚构）。
"""
import asyncio
import json
import os
import types
import unittest

import sys

# 复用已加载的那一份：同一文件按两个模块名各加载一次，会互相覆盖 astrbot 空壳（出站挂钩登记到另一份里）
T = sys.modules.get("test_reach_out") or sys.modules.get("tests.test_reach_out")
if T is None:
    try:
        from tests import test_reach_out as T
    except ImportError:  # discover -s tests 时
        import test_reach_out as T

L, M, run = T.L, T.MIND, T.run
PARTNER, FRIEND, OTHER, GROUP_A, GROUP_B = T.PARTNER, T.FRIEND, T.OTHER, T.GROUP_A, T.GROUP_B
H = 3600


def ev_group(sender=FRIEND, name="小明", group=GROUP_A, **k):
    return T.FakeEvent(sender=sender, name=name, group=group, **k)


def ev_private(sender=FRIEND, name="小明", **k):
    return T.FakeEvent(sender=sender, name=name, **k)


class Base(T.Base):
    def keep(self, event, kind="牵挂", about="here", text="明天考试", due=""):
        return run(self.p.do_keep_in_mind(event, kind, about, text, due))

    def inject(self, event):
        req = T.Req()
        run(self.p.on_llm_request(event, req))
        return req.system_prompt

    def items(self, finished=False):
        return run(self.p.mind.items(include_finished=finished))


class TestKeep(Base):
    def test_keep_here_in_group_is_about_speaker(self):
        r = self.keep(ev_group())
        self.assertIn("记在心上了", r)
        it = self.items()[0]
        self.assertEqual((it["about"], it["about_name"], it["origin"], it["kind"]),
                         (f"user:{FRIEND}", "小明", f"group:{GROUP_A}", "牵挂"))
        self.assertIsNone(it["due_at"])
        self.assertAlmostEqual(it["expires_at"] - it["created_at"], 72 * H)

    def test_about_variants(self):
        self.keep(ev_group(), about="group", text="群里周末团建")
        self.keep(ev_group(), about="partner", kind="想分享", text="跟搭档说群里要团建")
        self.keep(ev_group(), about=f"user:{OTHER}", text="小红感冒了")
        self.keep(T.FakeEvent(sender=PARTNER, admin=True), kind="约定", text="帮搭档查规则")
        abouts = {i["about"]: i["about_name"] for i in self.items()}
        self.assertEqual(abouts, {f"group:{GROUP_A}": f"群 {GROUP_A}", f"user:{PARTNER}": "搭档",
                                  f"user:{OTHER}": OTHER})

    def test_bad_inputs(self):
        e = ev_group()
        self.assertIn("kind 只能是", self.keep(e, kind="心情"))
        self.assertIn("没记下", self.keep(e, text="  "))
        self.assertIn("太长", self.keep(e, text="啊" * 81))
        self.assertIn("about 只能是", self.keep(e, about="小明"))
        self.assertIn("about 只能是", self.keep(e, about="user:abc"))
        self.assertIn("不在群里", self.keep(ev_private(), about="group"))
        self.assertIn("要是数字", self.keep(e, due="明天"))
        self.assertIn("负数", self.keep(e, due="-3"))
        self.make(admins=())
        self.assertIn("还没有设置搭档", self.keep(e, about="partner"))
        self.assertEqual(self.items(), [])

    def test_group_thought_has_no_speaker(self):
        e = ev_group(sender="0", name="伽拉忒亚的念头", extras={"reach_out_thought": True})
        self.assertIn("念头里没有说话人", self.keep(e))
        self.assertIn("记在心上了", self.keep(e, about="group", text="大家周末团建"))

    def test_due_and_clamps(self):
        self.keep(ev_group(), due="24")
        it = self.items()[0]
        self.assertAlmostEqual(it["due_at"] - it["created_at"], 24 * H)
        self.assertAlmostEqual(it["expires_at"] - it["due_at"], 24 * H)
        self.keep(ev_group(), text="下个月搬家", due="99999")
        it = [i for i in self.items() if "搬家" in i["text"]][0]
        self.assertAlmostEqual(it["due_at"] - it["created_at"], 336 * H)  # 最长 14 天
        self.assertAlmostEqual(it["expires_at"] - it["created_at"], 336 * H)
        self.keep(ev_group(), text="现在就该问", due="0")
        it = [i for i in self.items() if "现在" in i["text"]][0]
        self.assertEqual(it["due_at"], it["created_at"])

    def test_similar_merges(self):
        self.keep(ev_group(), text="明天考试")
        r = self.keep(ev_group(), text="明天考数学", due="20")
        self.assertIn("已更新", r)
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.items()[0]["text"], "明天考数学")
        self.assertIsNotNone(self.items()[0]["due_at"])
        # 不同的人、不同的 kind 不合并
        self.keep(ev_group(sender=OTHER, name="小红"), text="明天考试")
        self.keep(ev_group(), kind="想分享", text="明天考试")
        self.assertEqual(len(self.items()), 3)

    def test_limits_evict_least_urgent(self):
        self.make(cfg={"mind_max_total": 3, "mind_max_per_about": 2})
        self.keep(ev_group(), text="甲事", due="5")
        self.clock.t += 1
        self.keep(ev_group(), text="乙事情况")
        self.clock.t += 1
        self.keep(ev_group(), text="丙件东西")  # 小明超过 2 条：挤掉没到期时间、最旧的“乙”
        texts = sorted(i["text"] for i in self.items())
        self.assertEqual(texts, ["丙件东西", "甲事"])
        dropped = [i for i in self.items(finished=True) if i["status"] == "dropped"]
        self.assertEqual([i["text"] for i in dropped], ["乙事情况"])
        for n, name in ((OTHER, "小红"), ("40004", "小刚")):
            self.clock.t += 1
            self.keep(ev_group(sender=n, name=name), text=f"{name}的事")
        self.assertEqual(len(self.items()), 3)

    def test_concurrent_keep_respects_limit(self):
        self.make(cfg={"mind_max_total": 5, "mind_max_per_about": 5})

        async def many():
            await asyncio.gather(*[self.p.do_keep_in_mind(ev_group(sender=str(40000 + i), name=f"人{i}"),
                                                          "牵挂", "here", f"第{i}件不同的事情{i * 7}", "")
                                   for i in range(12)])
        run(many())
        self.assertEqual(len(self.items()), 5)


class TestLetGo(Base):
    def test_transitions(self):
        self.keep(ev_group())
        iid = self.items()[0]["id"]
        self.assertIn("办完了", run(self.p.do_let_go(ev_group(), iid, "done")))
        self.assertEqual(self.items(), [])
        self.assertIn("已经了结", run(self.p.do_let_go(ev_group(), iid, "drop")))
        self.keep(ev_group(), text="另一件事情")
        iid2 = self.items()[0]["id"]
        self.assertIn("放下了", run(self.p.do_let_go(ev_group(), iid2, "drop")))
        st = {i["id"]: i["status"] for i in self.items(finished=True)}
        self.assertEqual(st, {iid: "done", iid2: "dropped"})

    def test_bad(self):
        self.assertIn("how 只能是", run(self.p.do_let_go(ev_group(), "m00000", "忘了")))
        self.assertIn("没有 m00000", run(self.p.do_let_go(ev_group(), "m00000", "done")))

    def test_expire_and_cleanup(self):
        self.keep(ev_group())
        self.clock.t += 72 * H + 1
        self.assertEqual(self.items(), [])
        self.assertEqual(self.items(finished=True)[0]["status"], "expired")
        self.clock.t += 3 * 24 * H + 1
        self.assertEqual(self.items(finished=True), [])


class TestInject(Base):
    def test_surfaces_in_speakers_private_chat_not_in_origin_right_away(self):
        self.keep(ev_group(), text="明天考试")
        self.assertNotIn("【你心里挂着的事】", self.inject(ev_group(sender=OTHER, name="小红")))
        # 同一个群里 4 小时内不重复提（上下文本来就有）
        self.assertNotIn("明天考试", self.inject(ev_group()))
        out = self.inject(ev_private())
        self.assertIn("【你心里挂着的事】", out)
        self.assertIn("牵挂·小明：明天考试", out)
        self.assertIn(f"群 {GROUP_A}", out)
        self.assertIn("还没提过", out)

    def test_anti_nag_gap_and_cap(self):
        self.keep(ev_group(), text="明天考试")
        self.assertIn("明天考试", self.inject(ev_private()))
        self.assertNotIn("明天考试", self.inject(ev_private()))  # 8 小时内不再提
        self.clock.t += 8 * H
        out = self.inject(ev_private())
        self.assertIn("之前提起过 1 次", out)
        self.clock.t += 8 * H
        self.assertIn("明天考试", self.inject(ev_private()))
        self.clock.t += 8 * H
        self.assertNotIn("明天考试", self.inject(ev_private()))  # 3 次后不再注入

    def test_due_exception(self):
        self.keep(ev_group(), text="明天考试", due="40")
        for _ in range(3):
            self.inject(ev_private())
            self.clock.t += 8 * H + 1
        self.assertEqual(self.items()[0]["surfaced"], 3)
        self.assertNotIn("明天考试", self.inject(ev_private()))  # 还没到期，已提满 3 次
        self.clock.t += 16 * H  # 过了到期时间（40 小时）
        # 到期：即使已提 3 次，也例外提一次，而且在搭档私聊也能看到（到期的事）
        out = self.inject(T.FakeEvent(sender=PARTNER, admin=True))
        self.assertIn("明天考试", out)
        self.assertIn("到时候了", out)
        self.assertNotIn("明天考试", self.inject(T.FakeEvent(sender=PARTNER, admin=True)))

    def test_only_relevant_or_due(self):
        self.keep(ev_group(sender=OTHER, name="小红"), text="小红感冒了")
        # 在和小明私聊时，不提关于小红、未到期的事
        self.assertNotIn("小红感冒了", self.inject(ev_private()))

    def test_limit_and_order(self):
        self.make(cfg={"mind_inject_max": 2})
        self.keep(ev_group(), about="group", text="群里要团建了")
        self.keep(ev_group(), text="小明明天考试")
        self.keep(ev_group(), kind="想分享", text="给小明看猫的照片")
        out = self.inject(ev_group(group=GROUP_A, sender=FRIEND))
        self.assertEqual(out, "人格")  # 都是 4 小时内在本群记的
        self.clock.t += 4 * H + 1
        out = self.inject(ev_group())
        lines = [l for l in out.splitlines() if l.startswith("- [")]
        self.assertEqual(len(lines), 2)
        self.assertIn("小明", lines[0])  # 先说话人，再群

    def test_thought_turn_in_group_has_no_speaker_items(self):
        self.keep(ev_group(), text="小明明天考试")
        self.keep(ev_group(), about="group", text="群里要团建了")
        self.clock.t += 4 * H + 1
        out = self.inject(ev_group(sender="0", name="伽拉忒亚的念头", extras={"reach_out_thought": True}))
        self.assertIn("群里要团建了", out)
        self.assertNotIn("小明明天考试", out)

    def test_inject_failure_does_not_raise(self):
        async def boom(*a, **k):
            raise RuntimeError("x")
        self.p.mind.pick = boom
        self.assertEqual(self.inject(ev_private()), "人格")


class TestPrivacy(Base):
    """§4.5 矩阵：搭档私聊看全部；和 X 私聊看 X 私聊里记的 + 群里记的关于 X 的；群里只看群里记的。"""

    def setUp(self):
        super().setUp()
        self.keep(T.FakeEvent(sender=PARTNER, admin=True), about="partner", text="搭档在看医生")  # 搭档私聊
        self.keep(ev_private(sender=OTHER, name="小红"), about="here", text="小红在准备面试")  # 小红私聊
        self.keep(ev_group(), about=f"user:{OTHER}", text="小红在群里说要搬家")  # 群里，关于小红
        self.keep(ev_group(), about="group", text="群里周末团建")  # 群里，关于群
        self.clock.t += 4 * H + 1

    def seen(self, event, about_items=None):
        # 用 visible 直接检查矩阵（注入还受“和眼前有关”筛选，单独测）
        key = self.p._key(event)
        viewer = self.p._viewer(event)
        return {i["text"] for i in self.items() if M.visible(i, key, viewer, [PARTNER])}

    def test_partner_sees_all(self):
        self.assertEqual(self.seen(T.FakeEvent(sender=PARTNER, admin=True)),
                         {"搭档在看医生", "小红在准备面试", "小红在群里说要搬家", "群里周末团建"})

    def test_private_with_x(self):
        self.assertEqual(self.seen(ev_private(sender=OTHER, name="小红")),
                         {"小红在准备面试", "小红在群里说要搬家"})
        self.assertEqual(self.seen(ev_private()), set())

    def test_group(self):
        self.assertEqual(self.seen(ev_group(group=GROUP_B)), {"小红在群里说要搬家", "群里周末团建"})

    def test_x_private_never_in_group_even_when_x_speaks(self):
        out = self.inject(ev_group(sender=OTHER, name="小红"))
        self.assertNotIn("面试", out)
        self.assertIn("搬家", out)


class FakeProvider:
    def __init__(self, text="[]", exc=None, delay=0):
        self.text, self.exc, self.delay = text, exc, delay
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, **k):
        self.calls.append((prompt, system_prompt))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return types.SimpleNamespace(completion_text=self.text)


class TestSweep(Base):
    def setUp(self):
        super().setUp()
        self.prov = FakeProvider()
        self.ctx.get_provider_by_id = lambda pid: self.prov if pid == "deepseek/deepseek-flash" else None

    def chat(self):
        self.inject(ev_group(name="小明", msg="[小明(20002)] 明天考试好紧张"))  # 记一条近况（带 uid）
        run(self.p.on_outbound(T.outbound(True, GROUP_A, T.Plain("加油"))))

    def test_parse_validates_origin_and_about(self):
        entries = [{"key": f"group:{GROUP_A}", "uid": FRIEND, "who": "小明", "text": "明天考试", "ts": 1}]
        good = {"kind": "牵挂", "origin": f"group:{GROUP_A}", "about": f"user:{FRIEND}", "text": "小明明天考试",
                "due_in_hours": 20}
        bad = [dict(good, origin=f"group:{GROUP_B}"), dict(good, about=f"user:{OTHER}"),
               dict(good, kind="约定"), dict(good, text=""), dict(good, text="啊" * 81), "不是对象"]
        out = M.parse_sweep("好的：" + json.dumps([good] + bad, ensure_ascii=False) + " 完", entries, 80)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["about"], out[0]["due_hours"]), (f"user:{FRIEND}", 20))
        self.assertEqual(M.parse_sweep("没有", entries, 80), [])
        self.assertEqual(M.parse_sweep("[坏 json", entries, 80), [])
        many = json.dumps([dict(good, text=f"事{i}") for i in range(5)], ensure_ascii=False)
        self.assertEqual(len(M.parse_sweep(many, entries, 80)), 3)

    def test_prompt_lists_allowed_abouts(self):
        entries = [{"key": f"group:{GROUP_A}", "uid": FRIEND, "who": "小明", "text": "明天考试", "ts": 1}]
        p = M.sweep_prompt(entries)
        self.assertIn(f"user:{FRIEND}", p)
        self.assertIn(f"group:{GROUP_A}", p)

    def test_run_sweep_keeps_candidates(self):
        self.chat()
        self.prov.text = json.dumps([{"kind": "牵挂", "origin": f"group:{GROUP_A}", "about": f"user:{FRIEND}",
                                      "text": "小明明天考试", "due_in_hours": None}], ensure_ascii=False)
        kept = run(self.p.run_sweep())
        self.assertEqual(len(kept), 1)
        it = self.items()[0]
        self.assertEqual((it["source"], it["about_name"], it["origin"]), ("sweep", "小明", f"group:{GROUP_A}"))
        self.assertIn("回头想起来的", "\n".join(M.render([it], self.clock(), self.p._mind_place)))
        # 再跑一次：没有新近况，不调模型
        self.assertEqual(run(self.p.run_sweep()), [])
        self.assertEqual(len(self.prov.calls), 1)

    def test_run_sweep_skips_similar_existing(self):
        self.keep(ev_group(), text="明天考试")
        self.chat()
        self.prov.text = json.dumps([{"kind": "牵挂", "origin": f"group:{GROUP_A}", "about": f"user:{FRIEND}",
                                      "text": "小明明天考试"}], ensure_ascii=False)
        self.assertEqual(run(self.p.run_sweep()), [])
        self.assertEqual(len(self.items()), 1)

    def test_run_sweep_failures_are_quiet(self):
        for prov in (FakeProvider(exc=RuntimeError("x")), FakeProvider(text="乱码"), None):
            self.ctx.get_provider_by_id = lambda pid, p=prov: p
            self.chat()
            self.assertEqual(run(self.p.run_sweep()), [])
        self.make(cfg={"sweep_timeout_seconds": 0})
        self.ctx.get_provider_by_id = lambda pid: FakeProvider(delay=0.05)
        self.chat()
        self.assertEqual(run(self.p.run_sweep()), [])
        self.assertEqual(self.items(), [])

    def test_no_human_words_no_call(self):
        run(self.p.on_outbound(T.outbound(True, GROUP_A, T.Plain("我自言自语"))))
        self.assertEqual(run(self.p.run_sweep()), [])
        self.assertEqual(self.prov.calls, [])

    def test_tick_slots_beijing_hours_and_daily_cap(self):
        # 1800000000 是北京时间 16:00，不在默认时段
        self.assertFalse(run(self.p.sweep_tick()))
        self.clock.t += 2 * H  # 18:00
        self.assertTrue(run(self.p.sweep_tick()))
        self.assertFalse(run(self.p.sweep_tick()))  # 同一时段只做一次
        self.clock.t += 4 * H  # 22:00
        self.assertTrue(run(self.p.sweep_tick()))
        self.make(cfg={"sweep_hours": "10，14,18,22,abc,25", "sweep_daily_max": 1})
        self.assertEqual(self.p._sweep_hours(), {10, 14, 18, 22})
        self.clock.t += 12 * H  # 第二天 10:00
        self.assertTrue(run(self.p.sweep_tick()))
        self.clock.t += 4 * H  # 14:00，但每天只 1 次
        self.assertFalse(run(self.p.sweep_tick()))

    def test_sweeper_starts_only_when_enabled(self):
        self.inject(ev_private())
        self.assertIsNone(self.p._sweeper)
        self.make(cfg={"sweep_enabled": True})

        async def go():
            await self.p.on_llm_request(ev_private(), T.Req())
            started = self.p._sweeper is not None
            await self.p.terminate()
            return started
        self.assertTrue(run(go()))


class TestMindPersistenceAndCommand(Base):
    def test_reload(self):
        self.keep(ev_group(), text="明天考试", due="24")
        self.inject(ev_private())
        p2 = self.make()
        it = run(p2.mind.items())[0]
        self.assertEqual((it["text"], it["surfaced"]), ("明天考试", 1))

    def test_corrupt(self):
        with open(os.path.join(self.tmp.name, "mind.json"), "w", encoding="utf-8") as f:
            f.write("{坏")
        p = self.make()
        self.assertEqual(run(p.mind.items()), [])
        self.assertTrue(any(n.startswith("mind.json.corrupt-") for n in os.listdir(self.tmp.name)))

    def test_list_command(self):
        self.assertIn("没挂着什么事", run(self.p.do_list_mind()))
        self.keep(ev_group(), text="明天考试", due="24")
        self.keep(ev_group(), text="另外一件很不同的事情")
        iid = [i for i in self.items() if "另外" in i["text"]][0]["id"]
        run(self.p.do_let_go(ev_group(), iid, "done"))
        out = run(self.p.do_list_mind())
        self.assertIn("牵挂·小明：明天考试", out)
        self.assertIn("到期", out)
        self.assertIn("〔办了〕", out)
        self.assertTrue(out.index("明天考试") < out.index("〔办了〕"))  # 挂着的在前


if __name__ == "__main__":
    unittest.main()
