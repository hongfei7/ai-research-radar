"""Web Search 采集器 —— 用 DuckDuckGo 搜索补充 RSS 盲区"""

import asyncio
import logging
import random
from urllib.parse import quote, urlparse, parse_qs

import httpx
from selectolax.parser import HTMLParser

from radar.collectors.base import Collector
from radar.collectors.rss import normalize_url, make_id
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred
from radar.utils import truncate

logger = logging.getLogger(__name__)

_MAX_RAW_SUMMARY = 800
_SEARCH_TIMEOUT = 20
_MAX_RETRIES = 3


def _extract_real_url(ddg_url: str) -> str:
    """从 DuckDuckGo 跳转 URL 中提取真实目标 URL"""
    if "uddg=" in ddg_url:
        parsed = urlparse(ddg_url)
        qs = parse_qs(parsed.query)
        real = qs.get("uddg", [""])[0]
        if real:
            return real
    return ddg_url




async def _search_duckduckgo_html(query: str, max_results: int = 5) -> list[dict]:
    """用 DuckDuckGo HTML 搜索，带指数退避重试"""
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    results = []

    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(
                timeout=_SEARCH_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Research-Radar/1.0)"},
            ) as client:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code == 403:
                    wait = (2 ** attempt) + random.uniform(0, 2)
                    logger.warning(f"DDG 403 for '{query}', attempt {attempt+1}/{_MAX_RETRIES}, waiting {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                tree = HTMLParser(resp.text)

                for el in tree.css(".result")[:max_results]:
                    a_tag = el.css_first("a.result__a")
                    snippet_el = el.css_first("a.result__snippet")
                    if a_tag:
                        link = a_tag.attributes.get("href", "")
                        title = a_tag.text(strip=True)
                        snippet = snippet_el.text(strip=True) if snippet_el else ""
                        if title and link:
                            results.append({"title": title, "url": link, "snippet": snippet})
                break  # 成功，退出重试循环
        except Exception as e:
            logger.error(f"DuckDuckGo search failed for '{query}' (attempt {attempt+1}): {e}")
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
    return results[:max_results]


class WebSearchCollector(Collector):
    """用搜索引擎发现 RSS 没有覆盖的新闻源"""

    def __init__(self, coverage: list[dict] | None = None):
        self.coverage = coverage or []

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        max_per_stock = params.get("max_per_stock", 2)
        stocks = [s for s in (self.coverage or []) if s.get("ticker")]  # 跳过 PRIVATE
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

            # 每个标的一条综合查询
            if any("\u4e00" <= c <= "\u9fff" for c in name):
                query = f"{name} AI 芯片 最新动态"
            else:
                query = f"{name} AI chip semiconductor latest"

            try:
                results = await _search_duckduckgo_html(query, max_results=max_per_stock)
                # 随机延迟 1.5-3.0s，避免被 DDG 限速
                delay = 1.5 + random.uniform(0, 1.5)
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"[web_search] Search failed for '{name}': {e}")
                continue

            for r in results:
                raw_url = r.get("url", "")
                url = _extract_real_url(raw_url)
                if not url:
                    continue
                norm_url = normalize_url(url)
                if norm_url in seen_urls:
                    continue
                seen_urls.add(norm_url)

                title = r.get("title", "").strip()
                snippet = r.get("snippet", "").strip()
                if not title:
                    continue

                # 跳过明显不相关的
                title_lower = title.lower()
                if any(w in title_lower for w in ["stock price", "股价", "股票行情", "yahoo finance"]):
                    continue

                item = Item(
                    id=make_id(url),
                    title=title,
                    url=url,
                    source=source_id,
                    source_type="tech",
                    published_at=fetched_at,
                    fetched_at=fetched_at,
                    raw_summary=truncate(snippet),
                    credibility=_source_cred(source_id),
                    image_url="",
                )
                items.append(item)

        logger.info(f"[{source_id}] Web search: {len(items)} results for {len(stocks)} stocks")
        return items
