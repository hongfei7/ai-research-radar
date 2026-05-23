"""Web Search 采集器 —— 用 DuckDuckGo 搜索补充 RSS 盲区"""

import asyncio
import hashlib
import logging
import re
from urllib.parse import quote, urlparse, parse_qs

import httpx
from selectolax.parser import HTMLParser

from radar.collectors.base import Collector
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred

logger = logging.getLogger(__name__)

_MAX_RAW_SUMMARY = 800
_SEARCH_TIMEOUT = 20


def _extract_real_url(ddg_url: str) -> str:
    """从 DuckDuckGo 跳转 URL 中提取真实目标 URL"""
    if "uddg=" in ddg_url:
        parsed = urlparse(ddg_url)
        qs = parse_qs(parsed.query)
        real = qs.get("uddg", [""])[0]
        if real:
            return real
    return ddg_url


def _make_id(url: str) -> str:
    real = _extract_real_url(url)
    norm = real.strip().lower().rstrip("/")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _truncate(text: str, max_len: int = _MAX_RAW_SUMMARY) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = " ".join(text.split())
    return text[:max_len]


async def _search_duckduckgo_html(query: str, max_results: int = 5) -> list[dict]:
    """用 DuckDuckGo HTML 搜索（免费、抓取结果页）"""
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    results = []
    try:
        async with httpx.AsyncClient(
            timeout=_SEARCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Research-Radar/1.0)"},
        ) as client:
            resp = await client.get(url, follow_redirects=True)
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
            alias = stock.get("aliases", [])
            if not name:
                continue

            # 用标的名称 + 关键别名搜索
            search_names = [name] + (alias[:1] if alias else [])
            for sname in search_names[:2]:  # 最多 2 个搜索词
                queries = [f"{sname} AI news"]
                if any("\u4e00" <= c <= "\u9fff" for c in sname):
                    queries.append(f"{sname} 人工智能 芯片")
                else:
                    queries.append(f"{sname} semiconductor chip")

                for query in queries:
                    try:
                        results = await _search_duckduckgo_html(query, max_results=max_per_stock)
                        await asyncio.sleep(0.5)  # 避免被限速
                    except Exception as e:
                        logger.error(f"[web_search] Search failed for '{sname}': {e}")
                        continue

                    for r in results:
                        raw_url = r.get("url", "")
                        url = _extract_real_url(raw_url)
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
