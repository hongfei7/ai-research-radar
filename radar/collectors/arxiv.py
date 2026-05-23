"""arXiv 采集器 —— export.arxiv.org Atom API"""

import logging
from urllib.parse import urlencode

import httpx
import feedparser

from radar.collectors.base import Collector
from radar.collectors.rss import make_id, normalize_url
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred

logger = logging.getLogger(__name__)

_ARXIV_API = "https://export.arxiv.org/api/query"
_TIMEOUT = 30
_MAX_RAW_SUMMARY = 1500


def _parse_arxiv_date(entry) -> str:
    """从 arXiv Atom entry 提取 published 时间"""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val is not None:
            try:
                from datetime import datetime, timezone
                dt = datetime(*val[:6], tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                continue
    return utcnow_iso()


def _extract_arxiv_summary(entry) -> str:
    """从 arXiv entry 提取摘要，去 HTML 标签"""
    import re
    candidates = []
    if hasattr(entry, "summary"):
        candidates.append(entry.summary)
    if hasattr(entry, "content") and entry.content:
        candidates.append(entry.content[0].get("value", ""))

    for text in candidates:
        if text:
            clean = re.sub(r"<[^>]+>", "", text)
            clean = " ".join(clean.split())
            if len(clean) > 20:
                return clean[:_MAX_RAW_SUMMARY]
    return ""


def _get_arxiv_authors(entry) -> str:
    """提取作者列表"""
    authors = []
    for author in getattr(entry, "authors", []) or []:
        name = getattr(author, "name", "")
        if name:
            authors.append(name)
    return ", ".join(authors[:5])


class ArxivCollector(Collector):
    """arXiv 论文采集 —— export.arxiv.org Atom API"""

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        categories = params.get("categories", ["cs.AI", "cs.CL", "cs.LG"])
        max_results = int(params.get("max", 60))

        items: list[Item] = []
        fetched_at = utcnow_iso()

        # 按分类逐个查询（arXiv API 不支持 OR 查询时按 submittedDate 排）
        for cat in categories:
            try:
                batch = await self._fetch_category(cat, max_results, source_id, fetched_at)
                items.extend(batch)
                logger.info(f"[{source_id}] arXiv {cat}: {len(batch)} papers")
            except Exception as e:
                logger.error(f"[{source_id}] arXiv {cat} failed: {e}")
                continue

        # 去重（同一论文可能属于多个分类）
        seen: set[str] = set()
        unique: list[Item] = []
        for it in items:
            if it.id not in seen:
                seen.add(it.id)
                unique.append(it)

        logger.info(f"[{source_id}] Total: {len(items)} → {len(unique)} unique papers")
        return unique

    async def _fetch_category(
        self, cat: str, max_results: int, source_id: str, fetched_at: str
    ) -> list[Item]:
        query = f"cat:{cat}"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        url = f"{_ARXIV_API}?{urlencode(params)}"

        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.text

        feed = feedparser.parse(content)
        if feed.bozo and not feed.entries:
            logger.warning(f"arXiv {cat}: bozo feed with no entries")
            return []

        items: list[Item] = []
        for entry in feed.entries:
            try:
                arxiv_id = getattr(entry, "id", "").strip()
                # arXiv ID 形如 http://arxiv.org/abs/XXXX.XXXXX
                arxiv_url = arxiv_id if arxiv_id.startswith("http") else f"https://arxiv.org/abs/{arxiv_id}"
                paper_id = arxiv_id.split("/abs/")[-1] if "/abs/" in arxiv_id else arxiv_id

                title = getattr(entry, "title", "").strip()
                authors = _get_arxiv_authors(entry)
                summary = _extract_arxiv_summary(entry)

                # 构造更丰富的标题和摘要
                full_title = title
                if authors:
                    full_title = f"{title} [{authors}]"

                item = Item(
                    id=make_id(arxiv_url),
                    title=full_title[:300],
                    url=normalize_url(arxiv_url),
                    source=f"{source_id}:{cat}",
                    source_type="tech",
                    published_at=_parse_arxiv_date(entry),
                    fetched_at=fetched_at,
                    raw_summary=summary,
                    credibility=_source_cred(f"{source_id}:{cat}"),
                )
                items.append(item)
            except Exception as e:
                logger.warning(f"arXiv {cat} entry parse failed: {e}")
                continue

        return items
