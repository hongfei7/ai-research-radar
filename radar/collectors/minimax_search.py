"""MiniMax Coding Plan 联网搜索采集器 —— 替代 DDG HTML 抓取

调用 MiniMax POST /v1/coding_plan/search REST API，底层为 Google 级搜索。
配额：150 次/5h。策略：每轮轮换 15 个标（共 32 标），每标每 2 轮搜索一次。
"""

import asyncio
import hashlib
import logging
import random
import re
import time
from urllib.parse import urlparse

from radar.collectors.base import Collector
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred
from radar.minimax_client import MinimaxClient

logger = logging.getLogger(__name__)

_MAX_RAW_SUMMARY = 800
_SEARCH_DELAY = 0.3
# 每轮搜索的标的上限（控制在配额 150 次/5h 以内）
# 10 轮/5h × PER_RUN_STOCKS + 备用 DDG 搜索 ≈ 150 次
_PER_RUN_STOCKS = 15


def _make_id(url: str) -> str:
    norm = url.strip().lower().rstrip("/")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _truncate(text: str, max_len: int = _MAX_RAW_SUMMARY) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = " ".join(text.split())
    return text[:max_len]


class MinimaxSearchCollector(Collector):
    """用 MiniMax Coding Plan 联网搜索 API 发现 RSS 盲区新闻"""

    def __init__(self, coverage: list[dict] | None = None):
        self.coverage = coverage or []

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        max_per_stock = params.get("max_per_stock", 3)
        stocks = [s for s in (self.coverage or []) if s.get("ticker")]  # 跳过 PRIVATE
        if not stocks:
            logger.warning("[minimax_search] No coverage stocks configured, skipping")
            return []

        # —— 轮换策略：每轮只搜索 PER_RUN_STOCKS 个标的，控制配额 ——
        # 使用当前时间片（每 30 分钟一个 slot）做确定性轮换
        slot = int(time.time() / 1800)  # 30 分钟窗口
        start_idx = (slot * _PER_RUN_STOCKS) % len(stocks)
        selected = []
        for i in range(min(_PER_RUN_STOCKS, len(stocks))):
            selected.append(stocks[(start_idx + i) % len(stocks)])
        logger.info(
            f"[{source_id}] Slot {slot}: searching {len(selected)}/{len(stocks)} stocks "
            f"(indices {start_idx}-{(start_idx + len(selected) - 1) % len(stocks)})"
        )

        client = MinimaxClient()
        try:
            items: list[Item] = []
            fetched_at = utcnow_iso()
            seen_urls: set[str] = set()

            for stock in selected:
                name = stock.get("name", "")
                if not name:
                    continue

                # 构造搜索查询
                if any("\u4e00" <= c <= "\u9fff" for c in name):
                    query = f"{name} AI 芯片 最新"
                else:
                    query = f"{name} AI chip semiconductor latest news"

                try:
                    results = await client.search(query)
                    await asyncio.sleep(_SEARCH_DELAY)
                except Exception as e:
                    logger.error(f"[minimax_search] Search failed for '{name}': {e}")
                    continue

                for r in results[:max_per_stock]:
                    link = r.get("link", "")
                    if not link or link in seen_urls:
                        continue
                    seen_urls.add(link)

                    title = (r.get("title", "") or "").strip()
                    snippet = (r.get("snippet", "") or "").strip()
                    if not title:
                        continue

                    # 跳过不相关结果
                    title_lower = title.lower()
                    if any(w in title_lower for w in [
                        "stock price", "股价", "股票行情", "yahoo finance", "dividend",
                    ]):
                        continue

                    item = Item(
                        id=_make_id(link),
                        title=title,
                        url=link,
                        source=source_id,
                        source_type="tech",
                        published_at=r.get("date", fetched_at) or fetched_at,
                        fetched_at=fetched_at,
                        raw_summary=_truncate(snippet),
                        credibility=_source_cred(source_id),
                    )
                    items.append(item)

            logger.info(
                f"[{source_id}] MiniMax Search: {len(items)} results for {len(selected)} stocks"
            )
            return items

        finally:
            await client.close()
