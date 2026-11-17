# -*- coding: utf-8 -*-
"""联网搜索兜底：给模型一个 search_web 工具，走 DeepSeek Search API。

- 只注册一个工具，没有消息钩子、不往请求里注入任何内容、不自己发消息。
- Key 复用 AstrBot 里 DeepSeek 对话提供商的 Key（配置 provider_id），插件自己不存密钥。
- 结果是资料不是指令；每天调用次数有上限，出错时返回一句说明给模型，不抛异常。
"""
import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from . import search as S
except ImportError:  # 测试里按文件加载
    import search as S


async def _aiohttp_post(url, headers, payload, timeout):
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            return resp.status, data


@register(
    "astrbot_plugin_deepseek_search",
    "dafeiyu-tools",
    "联网搜索兜底：search_web 工具，走 DeepSeek Search API，复用 DeepSeek 的 Key",
    "1.0.0",
)
class DeepSeekSearchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.settings = S.Settings.from_config(config)
        self.budget = S.DailyBudget(self.settings.daily_limit)
        self._post = _aiohttp_post

    def _credentials(self):
        provider = self.context.get_provider_by_id(self.settings.provider_id)
        if provider is None:
            return None, None
        try:
            keys = [k for k in (provider.get_keys() or []) if k]
        except Exception:
            keys = []
        base = (getattr(provider, "provider_config", None) or {}).get("api_base", "")
        return (keys[0] if keys else None), base

    async def run(self, query) -> str:
        q = S.clean_query(query)
        if not q:
            return "搜索失败：搜索词是空的。"
        key, base = self._credentials()
        if not key:
            logger.warning(f"[deepseek_search] 找不到提供商 {self.settings.provider_id} 或它没有 Key")
            return "搜索失败：联网搜索没配好。"
        if not self.budget.take():
            return "今天的联网搜索次数用完了，先凭已知的回答，拿不准就直说。"
        url = S.search_url(base)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            status, data = await self._post(
                url, headers, S.build_payload(q, self.settings.result_count), self.settings.timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning("[deepseek_search] 超时")
            return "搜索失败：超时了。"
        except Exception as e:  # 网络异常不影响对话
            logger.warning(f"[deepseek_search] 请求异常：{type(e).__name__}")
            return "搜索失败：连不上搜索服务。"
        if status != 200:
            logger.warning(f"[deepseek_search] HTTP {status}")
            return S.error_text(status, data)
        text = S.format_results(data, self.settings)
        logger.info(f"[deepseek_search] 查询 {len(q)} 字，今日第 {self.budget.used} 次")
        return text

    @filter.llm_tool(name="search_web")
    async def search_web(self, event: AstrMessageEvent, query: str):
        """联网搜索，是最后的手段。先查知识库；动画、角色用 search_anime；都查不到，或者问的是最近的新闻、新内容时才用。跑团规则不要联网查。

        Args:
            query(string): 搜索关键词，简短具体，不超过 128 字
        """
        return await self.run(query)
