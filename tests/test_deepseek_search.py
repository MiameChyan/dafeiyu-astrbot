# -*- coding: utf-8 -*-
"""deepseek_search 插件回归：search_web 工具走 DeepSeek Search API。

跑法（纯标准库，不需要 AstrBot、不联网）：python -m unittest tests.test_deepseek_search
原理：纯逻辑直接测 search.py；插件入口用空壳 astrbot 模块 + 假的 HTTP post。
"""
import asyncio
import datetime as dt
import importlib.util
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugin", "astrbot_plugin_deepseek_search")
KEY = "sk-test-not-a-real-key"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load("deepseek_search_search", os.path.join(PLUGIN, "search.py"))


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

    class EventMessageType:
        ALL = "all"

    @staticmethod
    def event_message_type(*a, **k):
        return lambda f: f

    @classmethod
    def llm_tool(cls, name=None, **k):
        def deco(f):
            cls.tools[name] = f
            return f
        return deco


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
_stub("astrbot.api.event", AstrMessageEvent=object, filter=_Filter)
_stub("astrbot.api.star", Context=object, Star=_Star, register=lambda *a, **k: (lambda c: c))
sys.modules["astrbot.api"].event = sys.modules["astrbot.api.event"]
sys.modules["astrbot.api"].star = sys.modules["astrbot.api.star"]
sys.modules["search"] = S
main = _load("deepseek_search_main", os.path.join(PLUGIN, "main.py"))
# 其他测试文件可能先换掉了 filter；main 已经绑定好，这里恢复别人的不影响


class FakeProvider:
    def __init__(self, keys=(KEY,), api_base="https://api.deepseek.com/v1"):
        self._keys = list(keys)
        self.provider_config = {"api_base": api_base}

    def get_keys(self):
        return self._keys


class FakeContext:
    def __init__(self, providers):
        self.providers = providers

    def get_provider_by_id(self, pid):
        return self.providers.get(pid)


class FakePost:
    def __init__(self, status=200, data=None, exc=None):
        self.status, self.data, self.exc = status, data, exc
        self.calls = []

    async def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        if self.exc:
            raise self.exc
        return self.status, self.data


def result(i, **over):
    r = {"title": f"标题{i}", "url": f"https://e.com/{i}", "snippet": f"短摘要{i}",
         "summary": f"给模型读的摘要{i}", "site_name": "站点", "date": "2026-11-13T08:00:00Z",
         "rerank_score": 0.9, "authority": 2}
    r.update(over)
    return r


def plugin(config=None, providers=None, post=None):
    ctx = FakeContext({"deepseek/deepseek-flash": FakeProvider()} if providers is None else providers)
    p = main.DeepSeekSearchPlugin(ctx, config or {})
    p._post = post or FakePost(data={"results": [result(1)]})
    return p


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 纯逻辑
class LogicTests(unittest.TestCase):
    def test_search_url_strips_v1_and_slash(self):
        for base in ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/",
                     "https://api.deepseek.com", "https://api.deepseek.com/"):
            self.assertEqual(S.search_url(base), "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(S.search_url(""), "https://api.deepseek.com/v1/tools/search")

    def test_clean_query_collapses_and_caps(self):
        self.assertEqual(S.clean_query("  初音  未来\n生日 "), "初音 未来 生日")
        self.assertEqual(len(S.clean_query("字" * 300)), 128)
        self.assertEqual(S.clean_query(None), "")

    def test_payload_never_asks_for_full_content(self):
        self.assertEqual(S.build_payload("q", 5), {"query": "q", "count": 5, "need_content": False})

    def test_settings_clamp_and_bad_values(self):
        s = S.Settings.from_config({"result_count": 99, "max_chars_per_result": 5,
                                    "daily_limit": -3, "timeout_seconds": "abc", "provider_id": " x "})
        self.assertEqual((s.result_count, s.max_chars_per_result, s.daily_limit), (10, 100, 0))
        self.assertEqual(s.timeout_seconds, 15.0)
        self.assertEqual(s.provider_id, "x")
        self.assertEqual(S.Settings.from_config(None).provider_id, "deepseek/deepseek-flash")
        self.assertEqual(S.Settings.from_config({"provider_id": ""}).provider_id, "deepseek/deepseek-flash")

    def test_format_prefers_summary_and_marks_as_data(self):
        text = S.format_results({"results": [result(1)]}, S.Settings())
        self.assertTrue(text.startswith(S.HEADER))
        self.assertIn("给模型读的摘要1", text)
        self.assertNotIn("短摘要1", text)
        self.assertIn("[1] 标题1（站点｜2026-11-13）", text)

    def test_format_falls_back_to_snippet_and_skips_empty(self):
        data = {"results": [result(1, summary=""), {"title": "", "summary": ""}, "junk", result(3)]}
        text = S.format_results(data, S.Settings())
        self.assertIn("短摘要1", text)
        self.assertIn("[2] 标题3", text, "空条目和非 dict 不占编号")

    def test_format_limits_count_and_length(self):
        data = {"results": [result(i, summary="长" * 1000) for i in range(8)]}
        text = S.format_results(data, S.Settings(result_count=3, max_chars_per_result=100))
        self.assertIn("[3]", text)
        self.assertNotIn("[4]", text)
        self.assertNotIn("长" * 100, text)
        self.assertIn("长" * 99 + "…", text)

    def test_format_empty_and_malformed(self):
        self.assertEqual(S.format_results({"results": []}, S.Settings()), "没有搜到相关结果。")
        self.assertEqual(S.format_results(None, S.Settings()), "搜索失败：返回格式不对。")
        self.assertEqual(S.format_results({"x": 1}, S.Settings()), "搜索失败：返回格式不对。")

    def test_budget_limit_and_day_rollover(self):
        day = [dt.date(2026, 11, 18)]
        b = S.DailyBudget(2, today=lambda: day[0])
        self.assertTrue(b.take())
        self.assertTrue(b.take())
        self.assertFalse(b.take())
        day[0] = dt.date(2026, 11, 19)
        self.assertTrue(b.take(), "过了零点重新计数")

    def test_budget_zero_means_unlimited(self):
        b = S.DailyBudget(0)
        self.assertTrue(all(b.take() for _ in range(1000)))

    def test_error_texts(self):
        self.assertIn("Key 无效", S.error_text(401, None))
        self.assertIn("余额不足", S.error_text(402, None))
        self.assertIn("太频繁", S.error_text(429, {}))
        self.assertEqual(S.error_text(500, {"error": {"message": "boom"}}), "搜索失败：HTTP 500，boom。")


# ---------------------------------------------------------------- 插件入口
class ToolTests(unittest.TestCase):
    def setUp(self):
        LOG.lines.clear()

    def test_tool_registered_under_search_web(self):
        self.assertIs(_Filter.tools.get("search_web"), main.DeepSeekSearchPlugin.search_web)

    def test_happy_path_through_tool_entry(self):
        post = FakePost(data={"results": [result(1), result(2)]})
        p = plugin(post=post)
        text = run(p.search_web(object(), "  最新 新番 "))
        self.assertIn("给模型读的摘要2", text)
        url, headers, payload, timeout = post.calls[0]
        self.assertEqual(url, "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(payload, {"query": "最新 新番", "count": 5, "need_content": False})
        self.assertEqual(timeout, 15.0)

    def test_key_never_leaks_into_reply_or_logs(self):
        for post in (FakePost(data={"results": [result(1)]}), FakePost(status=401, data={"error": KEY}),
                     FakePost(exc=RuntimeError(KEY))):
            text = run(plugin(post=post).search_web(object(), "q"))
            self.assertNotIn(KEY, text)
        self.assertFalse(any(KEY in line for line in LOG.lines))

    def test_uses_configured_provider(self):
        post = FakePost(data={"results": [result(1)]})
        providers = {"other": FakeProvider(keys=("k2",), api_base="https://proxy.example/v1")}
        run(plugin({"provider_id": "other"}, providers=providers, post=post).search_web(object(), "q"))
        self.assertEqual(post.calls[0][0], "https://proxy.example/v1/tools/search")
        self.assertEqual(post.calls[0][1]["Authorization"], "Bearer k2")

    def test_missing_provider_or_key_does_not_call(self):
        for providers in ({}, {"deepseek/deepseek-flash": FakeProvider(keys=())},
                          {"deepseek/deepseek-flash": FakeProvider(keys=("",))}):
            post = FakePost()
            text = run(plugin(providers=providers, post=post).search_web(object(), "q"))
            self.assertEqual(text, "搜索失败：联网搜索没配好。")
            self.assertEqual(post.calls, [])

    def test_empty_query_does_not_call_or_spend_budget(self):
        post = FakePost()
        p = plugin({"daily_limit": 1}, post=post)
        self.assertEqual(run(p.search_web(object(), "   ")), "搜索失败：搜索词是空的。")
        self.assertEqual(post.calls, [])
        self.assertNotIn("次数用完", run(p.search_web(object(), "q")))

    def test_daily_limit_stops_calls(self):
        post = FakePost(data={"results": [result(1)]})
        p = plugin({"daily_limit": 2}, post=post)
        run(p.search_web(object(), "a"))
        run(p.search_web(object(), "b"))
        text = run(p.search_web(object(), "c"))
        self.assertIn("次数用完", text)
        self.assertEqual(len(post.calls), 2)

    def test_http_errors_become_text(self):
        self.assertIn("余额不足", run(plugin(post=FakePost(status=402)).search_web(object(), "q")))
        self.assertIn("HTTP 503", run(plugin(post=FakePost(status=503)).search_web(object(), "q")))

    def test_timeout_and_network_errors_become_text(self):
        self.assertEqual(run(plugin(post=FakePost(exc=asyncio.TimeoutError())).search_web(object(), "q")),
                         "搜索失败：超时了。")
        self.assertEqual(run(plugin(post=FakePost(exc=OSError("down"))).search_web(object(), "q")),
                         "搜索失败：连不上搜索服务。")

    def test_200_with_bad_body(self):
        self.assertEqual(run(plugin(post=FakePost(data=None)).search_web(object(), "q")),
                         "搜索失败：返回格式不对。")

    def test_concurrent_calls_respect_limit(self):
        post = FakePost(data={"results": [result(1)]})
        p = plugin({"daily_limit": 3}, post=post)

        async def many():
            return await asyncio.gather(*(p.search_web(object(), f"q{i}") for i in range(10)))

        texts = run(many())
        self.assertEqual(len(post.calls), 3)
        self.assertEqual(sum("次数用完" in t for t in texts), 7)


if __name__ == "__main__":
    unittest.main()
