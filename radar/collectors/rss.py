"""通用 RSS 采集器"""

import hashlib
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from datetime import datetime, timezone

import httpx
import feedparser

from radar.collectors.base import Collector
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred

logger = logging.getLogger(__name__)

# 需要移除的跟踪参数
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source", "pk_campaign",
    "pk_kwd", "ck_subscriber_id",
}

# 请求超时
_TIMEOUT = 30

# raw_summary 最大字符数
_MAX_RAW_SUMMARY = 1500


def normalize_url(url: str) -> str:
    """URL 规范化：去跟踪参数、统一小写 scheme+netloc、去尾部斜杠"""
    if not url:
        return url
    parsed = urlparse(url)
    # 统一小写
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # 去尾部斜杠
    path = parsed.path.rstrip("/") or "/"
    # 过滤跟踪参数
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    clean_params = {k: v for k, v in query_params.items() if k not in _TRACKING_PARAMS}
    query = urlencode(clean_params, doseq=True)
    return urlunparse((scheme, netloc, path, parsed.params, query, parsed.fragment))


def make_id(url: str) -> str:
    """对规范化 URL 取 SHA1 作为唯一指纹"""
    norm = normalize_url(url)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _parse_date(entry) -> str:
    """从 feedparser entry 中提取 published 时间，返回 ISO8601"""
    # feedparser 的 published_parsed / updated_parsed 是 struct_time
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val is not None:
            try:
                dt = datetime(*val[:6], tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                continue
    return utcnow_iso()


def _extract_summary(entry) -> str:
    """提取原始摘要/正文片段，截断到 _MAX_RAW_SUMMARY 字符"""
    # 优先取 summary，其次 content.value，再其次 description
    candidates = []
    if hasattr(entry, "summary"):
        candidates.append(entry.summary)
    if hasattr(entry, "content") and entry.content:
        candidates.append(entry.content[0].get("value", ""))
    if hasattr(entry, "description"):
        candidates.append(entry.description)

    for text in candidates:
        if text:
            # 去除 HTML 标签（简单处理）
            import re
            clean = re.sub(r"<[^>]+>", "", text)
            clean = " ".join(clean.split())  # 压缩空白
            if len(clean) > 20:  # 有效内容至少 20 字符
                return clean[:_MAX_RAW_SUMMARY]

    return ""


class RSSCollector(Collector):
    """通用 RSS/Atom 采集器，供所有 RSS 信源复用"""

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        url = params.get("url", "")
        if not url:
            logger.warning(f"[{source_id}] No URL in params, skipping")
            return []

        items: list[Item] = []
        fetched_at = utcnow_iso()

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                content = resp.text
        except Exception as e:
            logger.error(f"[{source_id}] HTTP fetch failed: {e}")
            return []

        try:
            feed = feedparser.parse(content)
        except Exception as e:
            logger.error(f"[{source_id}] Feed parse failed: {e}")
            return []

        if feed.bozo and not feed.entries:
            logger.warning(f"[{source_id}] Bozo feed with no entries: {feed.bozo_exception}")
            return []

        for entry in feed.entries:
            try:
                link = getattr(entry, "link", "")
                if not link:
                    continue

                item = Item(
                    id=make_id(link),
                    title=getattr(entry, "title", "").strip(),
                    url=normalize_url(link),
                    source=source_id,
                    source_type=self._classify_type(source_id),
                    published_at=_parse_date(entry),
                    fetched_at=fetched_at,
                    raw_summary=_extract_summary(entry),
                    credibility=_source_cred(source_id),
                )
                items.append(item)
            except Exception as e:
                logger.warning(f"[{source_id}] Failed to process entry: {e}")
                continue

        logger.info(f"[{source_id}] Fetched {len(items)} entries from RSS")
        return items

    @staticmethod
    def _classify_type(source_id: str) -> str:
        """根据 source_id 分类为 tech 或 market"""
        market_ids = {"sec_edgar"}
        for mid in market_ids:
            if mid in source_id:
                return "market"
        return "tech"
