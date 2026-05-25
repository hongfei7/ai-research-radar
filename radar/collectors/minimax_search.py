"""MiniMax Coding Plan 联网搜索采集器 —— 替代 DDG HTML 抓取

调用 MiniMax POST /v1/coding_plan/search REST API，底层为 Google 级搜索。
配额：150 次/5h。策略：每轮轮换 15 个标（共 32 标），每标每 2 轮搜索一次。
"""

import asyncio
import logging
import time

from radar.collectors.base import Collector
from radar.collectors.rss import normalize_url, make_id
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred
from radar.minimax_client import MinimaxClient
from radar.utils import truncate

logger = logging.getLogger(__name__)

_MAX_RAW_SUMMARY = 800
_SEARCH_DELAY = 0.3
_PER_RUN_STOCKS = 10      # 每轮搜索标的数（32 标轮换）
_PER_RUN_TRENDING = 3     # 每轮搜索趋势话题数（10 话题轮换）


class MinimaxSearchCollector(Collector):
    """用 MiniMax Coding Plan 联网搜索 API 发现 RSS 盲区新闻
    同时追踪标的 + 趋势话题（替代已失效的 X/Twitter 信源）"""

    def __init__(self, coverage: list[dict] | None = None):
        self.coverage = coverage or []
        self.trending_topics: list[str] = []

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        max_per_stock = params.get("max_per_stock", 3)
        stocks = [s for s in (self.coverage or []) if s.get("ticker")]  # 跳过 PRIVATE
        topics = self.trending_topics or []

        # —— 轮换策略：时间片轮换标的 + 趋势话题 ——
        slot = int(time.time() / 1800)  # 30 分钟窗口

        # 标的轮换
        stock_start_idx = (slot * _PER_RUN_STOCKS) % max(len(stocks), 1)
        selected_stocks = []
        for i in range(min(_PER_RUN_STOCKS, len(stocks))):
            selected_stocks.append(stocks[(stock_start_idx + i) % len(stocks)])

        # 趋势话题轮换
        topic_start_idx = (slot * _PER_RUN_TRENDING) % max(len(topics), 1)
        selected_topics = []
        for i in range(min(_PER_RUN_TRENDING, len(topics))):
            selected_topics.append(topics[(topic_start_idx + i) % len(topics)])

        logger.info(
            f"[{source_id}] Slot {slot}: searching {len(selected_stocks)} stocks + "
            f"{len(selected_topics)} trending topics (total {len(selected_stocks) + len(selected_topics)} queries)"
        )

        client = MinimaxClient()
        try:
            items: list[Item] = []
            fetched_at = utcnow_iso()
            seen_urls: set[str] = set()
            queries: list[tuple[str, str]] = []  # (query, label)

            # 构造标的搜索查询
            for stock in selected_stocks:
                name = stock.get("name", "")
                if not name:
                    continue
                if any("\u4e00" <= c <= "\u9fff" for c in name):
                    queries.append((f"{name} AI 芯片 最新", name))
                else:
                    queries.append((f"{name} AI chip semiconductor latest news", name))

            # 趋势话题直接用
            for t in selected_topics:
                queries.append((t, t))

            for query, label in queries:
                try:
                    results = await client.search(query)
                    await asyncio.sleep(_SEARCH_DELAY)
                except Exception as e:
                    logger.error(f"[minimax_search] Search failed for '{label}': {e}")
                    continue

                for r in results[:max_per_stock]:
                    link = r.get("link", "")
                    if not link:
                        continue
                    norm_link = normalize_url(link)
                    if norm_link in seen_urls:
                        continue
                    seen_urls.add(norm_link)

                    title = (r.get("title", "") or "").strip()
                    snippet = (r.get("snippet", "") or "").strip()
                    if not title:
                        continue

                    # 提取图片 URL：尝试多种常见字段名
                    image_url = (
                        r.get("image") or r.get("thumbnail") or
                        r.get("og_image") or r.get("og:image") or ""
                    )
                    # 也尝试从 pagemap 提取（Google 风格搜索结果）
                    if not image_url:
                        pagemap = r.get("pagemap", {})
                        if isinstance(pagemap, dict):
                            cse = pagemap.get("cse_image") or pagemap.get("cse_thumbnail")
                            if isinstance(cse, list) and cse:
                                image_url = cse[0].get("src", "")
                            if not image_url:
                                metatags = pagemap.get("metatags")
                                if isinstance(metatags, list) and metatags:
                                    image_url = metatags[0].get("og:image", "")
                    image_url = image_url.strip() if image_url else ""

                    # 跳过不相关结果
                    title_lower = title.lower()
                    if any(w in title_lower for w in [
                        "stock price", "股价", "股票行情", "yahoo finance", "dividend",
                    ]):
                        continue

                    item = Item(
                        id=make_id(link),
                        title=title,
                        url=link,
                        source=source_id,
                        source_type="tech",
                        published_at=r.get("date", fetched_at) or fetched_at,
                        fetched_at=fetched_at,
                        raw_summary=truncate(snippet),
                        credibility=_source_cred(source_id),
                        image_url=image_url,
                    )
                    items.append(item)

            logger.info(
                f"[{source_id}] MiniMax Search: {len(items)} results "
                f"({len(queries)} queries: {len(selected_stocks)} stocks + {len(selected_topics)} topics)"
            )
            return items

        finally:
            await client.close()
