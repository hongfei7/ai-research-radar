"""MiniMax Web Search 采集器 —— 用网络搜索补充 RSS 盲区"""

import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from radar.collectors.base import Collector
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred

logger = logging.getLogger(__name__)

_MAX_RAW_SUMMARY = 800
_SEARCH_TIMEOUT = 20

# 每个标的关键词组合
_SEARCH_QUERIES = [
    # AI 芯片/硬件
    "{name} AI chip news today",
    "{name} semiconductor latest",
    # 云厂/AI 厂商
    "{name} AI model launch",
    "{name} partnership AI deal",
    # 国产替代
    "{name} 国产芯片 最新",
    "{name} AI 融资 合作",
]


def _make_id(url: str) -> str:
    norm = url.strip().lower().rstrip("/")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _truncate(text: str, max_len: int = _MAX_RAW_SUMMARY) -> str:
    text = " ".join(text.split())
    return text[:max_len]


async def _search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
    """用 DuckDuckGo Instant Answer API 搜索（免费、无需 Key）"""
    url = f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_html=1&skip_disambig=1"
    results = []
    try:
        async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
            # RelatedTopics 包含搜索结果
            for topic in data.get("RelatedTopics", [])[:max_results]:
                if isinstance(topic, dict):
                    text = topic.get("Text", "")
                    first_url = topic.get("FirstURL", "")
                    if text and first_url:
                        results.append({"title": text.split(" - ")[0], "url": first_url, "snippet": text})
            # 也检查 Abstract
            if data.get("Abstract") and data.get("AbstractURL"):
                results.append({
                    "title": data.get("Heading", ""),
                    "url": data.get("AbstractURL", ""),
                    "snippet": data.get("Abstract", ""),
                })
    except Exception as e:
        logger.error(f"DuckDuckGo search failed for '{query}': {e}")
    return results[:max_results]


class WebSearchCollector(Collector):
    """用搜索引擎发现 RSS 没有覆盖的新闻源"""

    def __init__(self, coverage: list[dict] | None = None):
        self.coverage = coverage or []

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        max_per_stock = params.get("max_per_stock", 3)
        stocks = self.coverage or []
        if not stocks:
            logger.warning("[web_search] No coverage stocks configured, skipping")
            return []

        items: list[Item] = []
        fetched_at = utcnow_iso()
        seen_urls: set[str] = set()

        for stock in stocks:
            name = stock.get("name", "")
            if not name:
                continue

            # 对每个标的搜 2 个 query（中英文各一）
            queries = [
                f"{name} AI chip semiconductor news",
                f"{name} AI 人工智能 最新 动态",
            ]

            for query in queries:
                try:
                    results = await _search_duckduckgo(query, max_results=max_per_stock)
                except Exception as e:
                    logger.error(f"[web_search] Search failed for '{name}': {e}")
                    continue

                for r in results:
                    url = r.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = r.get("title", "").strip()
                    snippet = r.get("snippet", "").strip()
                    if not title:
                        continue

                    # 跳过明显不相关的
                    title_lower = title.lower()
                    if any(w in title_lower for w in ["stock price", "股价", "股票行情", "yahoo finance"]):
                        continue

                    item = Item(
                        id=_make_id(url),
                        title=title,
                        url=url,
                        source=source_id,
                        source_type="tech",
                        published_at=fetched_at,
                        fetched_at=fetched_at,
                        raw_summary=_truncate(snippet),
                        credibility=_source_cred(source_id),
                    )
                    items.append(item)

        logger.info(f"[{source_id}] Web search: {len(items)} results for {len(stocks)} stocks")
        return items
