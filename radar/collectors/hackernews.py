"""Hacker News 采集器 —— Firebase API"""

import logging
import asyncio
from datetime import datetime, timezone

import httpx

from radar.collectors.base import Collector
from radar.collectors.rss import make_id, normalize_url
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred

logger = logging.getLogger(__name__)

_HN_API = "https://hacker-news.firebaseio.com/v0"
_TIMEOUT = 30
_MAX_CONCURRENT = 10          # 并发获取 item 细节
_MAX_RAW_SUMMARY = 1500


def _hn_timestamp(ts: int) -> str:
    """Unix timestamp → ISO8601"""
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return utcnow_iso()


def _is_recent_enough(ts: int, hours: int = 72) -> bool:
    """判断是否在最近 N 小时内"""
    try:
        now = datetime.now(timezone.utc).timestamp()
        return (now - ts) <= hours * 3600
    except Exception:
        return True


class HackerNewsCollector(Collector):
    """Hacker News —— Firebase API，取 top stories 中最近 72 小时的"""

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        story_type = params.get("story", "top")
        max_items = int(params.get("max", 50))
        fetched_at = utcnow_iso()

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Step 1: 获取 story ID 列表
            url = f"{_HN_API}/{story_type}stories.json"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                all_ids: list[int] = resp.json()
            except Exception as e:
                logger.error(f"[{source_id}] Failed to get story IDs: {e}")
                return []

            if not all_ids:
                logger.warning(f"[{source_id}] No story IDs returned")
                return []

            logger.debug(f"[{source_id}] Got {len(all_ids)} story IDs")

            # Step 2: 并发获取 item 细节
            items: list[Item] = []
            sem = asyncio.Semaphore(_MAX_CONCURRENT)

            async def fetch_one(story_id: int) -> Item | None:
                async with sem:
                    try:
                        item_url = f"{_HN_API}/item/{story_id}.json"
                        r = await client.get(item_url)
                        r.raise_for_status()
                        data = r.json()
                        if not data:
                            return None

                        # 跳过非 story 类型（job, poll 等）
                        if data.get("type") != "story":
                            return None

                        title = data.get("title", "").strip()
                        hn_url = data.get("url", "")
                        if not hn_url:
                            # Ask HN / Show HN 用 HN 自身链接
                            hn_url = f"https://news.ycombinator.com/item?id={story_id}"

                        # 时间过滤：最近 72 小时
                        ts = data.get("time", 0)
                        if not _is_recent_enough(ts, 72):
                            return None

                        # 摘要 = title + text（如有）
                        text = data.get("text", "") or ""
                        raw_summary = f"{title}. {text}" if text else title
                        raw_summary = raw_summary[:_MAX_RAW_SUMMARY]

                        return Item(
                            id=make_id(hn_url),
                            title=title,
                            url=normalize_url(hn_url),
                            source=source_id,
                            source_type="tech",
                            published_at=_hn_timestamp(ts),
                            fetched_at=fetched_at,
                            raw_summary=raw_summary,
                            credibility=_source_cred(source_id),
                        )
                    except Exception as e:
                        logger.debug(f"[{source_id}] Failed to fetch item {story_id}: {e}")
                        return None

            # 只取前 max_items*3 个 ID 做并发获取（多数会被时间过滤掉）
            candidate_ids = all_ids[: max_items * 3]
            tasks = [fetch_one(sid) for sid in candidate_ids]
            results = await asyncio.gather(*tasks)

            for r in results:
                if r is not None:
                    items.append(r)
                    if len(items) >= max_items:
                        break

        logger.info(f"[{source_id}] Fetched {len(items)} HN stories (from {len(candidate_ids)} candidates)")
        return items
