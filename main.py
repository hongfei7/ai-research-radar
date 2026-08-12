#!/usr/bin/env python3
"""AI 投研雷达 — 管道入口 - hongfei

六阶段管道: 采集 → 去重 → MiniMax筛选 → MiniMax提取 → 事件聚类 → 态势更新 → 存储 → 分发

Usage:
    python main.py --stage collect      # M1: 仅采集+去重+存储
    python main.py --stage process      # M2: 采集 + LLM处理(筛选+提取)
    python main.py --stage cluster      # M3: + 事件聚类
    python main.py --stage full         # M4+: 完整管道(含渲染+分发)
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from radar.config import load_config
from radar.models import Item, parse_iso, get_effective_date
from radar.collectors.rss import RSSCollector
from radar.collectors.arxiv import ArxivCollector
from radar.collectors.hackernews import HackerNewsCollector
from radar.collectors.github_trending import GithubTrendingCollector
from radar.collectors.sec_edgar import SECEdgarCollector
from radar.collectors.web_search import WebSearchCollector
from radar.collectors.minimax_search import MinimaxSearchCollector
from radar.collectors.huggingface_papers import HuggingFacePapersCollector
from radar.dedup import DedupStore
from radar.minimax_client import MinimaxClient
from radar.processor import Processor
from radar.cluster import ClusterEngine
from radar.situation import SituationGenerator
from radar.storage import save_items, load_events, save_events, load_situation, save_situation
from radar.textnorm import clean_title, is_digest_title

logger = logging.getLogger("radar")

COLLECTOR_MAP = {
    "rss": RSSCollector(),
    "arxiv": ArxivCollector(),
    "hackernews": HackerNewsCollector(),
    "github_trending": GithubTrendingCollector(),
    "sec_edgar": SECEdgarCollector(),
    "web_search": WebSearchCollector(),
    "minimax_search": MinimaxSearchCollector(),
    "huggingface_papers": HuggingFacePapersCollector(),
}

# 全局运行计数器（用于态势更新间隔）
_run_count = 0


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


async def collect_all(cfg: dict) -> list[Item]:
    """采集全部信源，返回去重后的新条目列表"""
    from datetime import datetime, timezone, timedelta

    dedup = DedupStore()

    # 注入 coverage 到需要标的列表的采集器
    sec_collector = COLLECTOR_MAP.get("sec_edgar")
    if isinstance(sec_collector, SECEdgarCollector):
        sec_collector.set_coverage(cfg.get("coverage", []))
    ws_collector = COLLECTOR_MAP.get("web_search")
    if isinstance(ws_collector, WebSearchCollector):
        ws_collector.coverage = cfg.get("coverage", [])
    ms_collector = COLLECTOR_MAP.get("minimax_search")
    if isinstance(ms_collector, MinimaxSearchCollector):
        ms_collector.coverage = cfg.get("coverage", [])
        ms_collector.trending_topics = cfg.get("trending_topics", [])

    default_window = cfg["runtime"].get("rolling_window_hours", 8)
    all_sources = []
    window_by_source: dict[str, float] = {}
    for src_type in ["tech", "market"]:
        for src in cfg["sources"].get(src_type, []):
            params = src.get("params", {})
            all_sources.append((src["id"], src["type"], params))
            window_by_source[src["id"]] = params.get("window_hours", default_window)

    digest_patterns = cfg["sources"].get("digest_title_patterns")
    digest_dropped: list[str] = []

    logger.info(f"Collecting from {len(all_sources)} sources (parallel)...")

    # 并发采集所有信源
    async def _fetch_one(src_id, src_type, params):
        collector = COLLECTOR_MAP.get(src_type)
        if collector is None:
            logger.warning(f"No collector for type '{src_type}' (source: {src_id}), skipping")
            return []
        try:
            items = await collector.fetch(src_id, params)
        except Exception as e:
            logger.error(f"[{src_id}] Collector failed: {e}")
            return []
        # 采集层统一出口: 清洗标题(站点后缀/话题标签/自我重复/全小写术语),
        # 并丢弃多话题聚合帖。放在这里而非各采集器内, 新增采集器自动受益;
        # 去重基于 URL, 改标题不影响幂等
        kept = []
        for it in items:
            it.title = clean_title(it.title)
            if is_digest_title(it.title, digest_patterns):
                digest_dropped.append(f"[{src_id}] {it.title[:40]}")
                continue
            kept.append(it)
        return kept

    results = await asyncio.gather(
        *[_fetch_one(src_id, src_type, params) for src_id, src_type, params in all_sources],
        return_exceptions=True,
    )

    all_items = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            src_id = all_sources[i][0]
            logger.error(f"[{src_id}] Collector exception: {result}")
        elif isinstance(result, list):
            all_items.extend(result)

    logger.info(f"Collected {len(all_items)} raw items total")

    if digest_dropped:
        logger.info(
            f"Digest filtered: {len(digest_dropped)} 聚合帖丢弃 "
            f"(样例: {digest_dropped[:3]})"
        )

    # —— 时间窗口过滤：按信源各自的窗口保留 ——
    # 单一全局窗口会误杀批量发布的信源: arXiv 每日批次、SEC 申报日期只到天、
    # HF Daily Papers 都天然落在 8h 之外, 而搜索类信源伪造 published_at=now 永远过窗,
    # 结果是高可信源整月零产出、低可信搜索结果占四成(审计发现)。
    now_utc = datetime.now(timezone.utc)
    stale_by_source: dict[str, int] = {}
    undated_by_source: dict[str, int] = {}
    filtered_items = []

    def _window_for(source: str) -> float:
        if source in window_by_source:
            return window_by_source[source]
        # 采集器可能派生子 source(如 "arxiv:cs.AI"、"sec_edgar:8-K")
        for src_id, hours in window_by_source.items():
            if source.startswith(src_id):
                return hours
        return default_window

    for it in all_items:
        pub_dt = get_effective_date(it)
        if pub_dt is None:
            # 无日期一律丢弃。放行等于把"未知"当成"新鲜": 实测搜索类信源翻出的
            # SEO 洗稿站文章正好没有日期, 一篇三个月前的旧闻就这样成了突发快报。
            # 除搜索类外的信源 100% 带可解析日期(实测 1260/1260), 丢弃无误杀风险。
            undated_by_source[it.source] = undated_by_source.get(it.source, 0) + 1
            continue

        if pub_dt >= now_utc - timedelta(hours=_window_for(it.source)):
            filtered_items.append(it)
        else:
            stale_by_source[it.source] = stale_by_source.get(it.source, 0) + 1

    stale_count = sum(stale_by_source.values())
    undated_count = sum(undated_by_source.values())
    if stale_count or undated_count:
        logger.info(
            f"Time filter: {stale_count} stale "
            f"(top: {sorted(stale_by_source.items(), key=lambda x: -x[1])[:5]}), "
            f"{undated_count} undated dropped "
            f"(top: {sorted(undated_by_source.items(), key=lambda x: -x[1])[:5]}), "
            f"{len(filtered_items)} remaining"
        )
    all_items = filtered_items

    new_items: list[Item] = []
    if all_items:
        all_ids = [it.id for it in all_items]
        new_ids = dedup.filter_new(all_ids)
        id_set = set(new_ids)
        new_items = [it for it in all_items if it.id in id_set]
        logger.info(f"Dedup: {len(all_items)} raw → {len(new_items)} new")
    else:
        new_items = []

    dedup.close()
    return new_items


async def run_collect(cfg: dict) -> None:
    """M1: 采集 + 去重 + 存储"""
    new_items = await collect_all(cfg)
    if new_items:
        save_items(new_items)
        dedup = DedupStore()
        dedup.mark_seen_batch([(it.id, it.title) for it in new_items])
        dedup.close()
    else:
        logger.info("No new items to save")
    logger.info("M1 collect stage done")


async def run_process(cfg: dict) -> None:
    """M2: 采集 + 去重 + MiniMax筛选 + MiniMax提取 + 存储"""
    new_items = await collect_all(cfg)
    if not new_items:
        logger.info("No new items to process")
        return

    client = MinimaxClient(model=cfg["minimax"]["model"])
    try:
        processor = Processor(client, cfg)
        processed = await processor.process(new_items)
    finally:
        await client.close()

    if processed:
        save_items(processed)

    # 将所有抓取的条目标记为已见（即使未通过筛选），避免重复 LLM 调用
    dedup = DedupStore()
    dedup.mark_seen_batch([(it.id, it.title) for it in new_items])
    dedup.close()

    logger.info(
        f"M2 process stage done: {len(new_items)} new → "
        f"{len(processed)} passed triage + extract"
    )


async def run_cluster(cfg: dict) -> tuple[list[Item], dict]:
    """M3: process + 事件聚类 + 存储"""
    new_items = await collect_all(cfg)
    if not new_items:
        logger.info("No new items to process")
        return [], {}

    client = MinimaxClient(model=cfg["minimax"]["model"])
    try:
        # Stage 1-2: triage + extract
        processor = Processor(client, cfg)
        processed = await processor.process(new_items)

        if not processed:
            # 条目标记为已见（即使未通过筛选），避免后续轮次重复 LLM 调用
            dedup = DedupStore()
            dedup.mark_seen_batch([(it.id, it.title) for it in new_items])
            dedup.close()
            return [], {}

        # Stage 3: 加载已有事件 → 聚类
        existing_events = load_events()
        cluster_engine = ClusterEngine(client, cfg)
        clustered_items, updated_events = await cluster_engine.cluster(
            processed, existing_events
        )

        # 存储
        save_items(clustered_items)
        save_events(updated_events)

        # 将所有抓取的条目标记为已见（即使未通过筛选）
        dedup = DedupStore()
        dedup.mark_seen_batch([(it.id, it.title) for it in new_items])
        dedup.close()

        logger.info(
            f"M3 cluster stage done: {len(new_items)} new → "
            f"{len(processed)} processed → {len(updated_events)} events"
        )
        return clustered_items, {"events": updated_events}

    finally:
        await client.close()


def _reapply_event_ttl(events: dict, ttl_hours: int) -> None:
    """重新评估事件 TTL，将过期事件标记为 inactive（用于 cold path）

    根据 last_updated_at 与当前时间差判断是否过期，
    过期则标记 is_active=False、status="resolved"。
    """
    now_dt = datetime.now(timezone.utc)
    changed = 0
    for event in events.values():
        if not event.is_active:
            continue
        ts = event.last_updated_at or event.first_seen_at
        if not ts:
            continue
        updated = parse_iso(ts)
        if updated is None:
            continue
        hours_since = (now_dt - updated).total_seconds() / 3600
        if hours_since >= ttl_hours:
            event.is_active = False
            event.status = "resolved"
            changed += 1
            logger.info(
                f"Event {event.event_id} resolved (stale {hours_since:.1f}h, cold path)"
            )
    if changed:
        logger.info(f"Cold path TTL cleanup: {changed} events marked resolved")


async def run_full(cfg: dict, notify_dry_run: bool = False) -> None:
    """M4+: 完整管道 → 采集+处理+聚类+态势+渲染+分发

    三条路径(无新条目/未过筛选/正常)在分发前汇合(审计 S1/F3):
    渲染与推送不再依赖"本轮是否有新条目", 晨报/速递在低流量时段不再缺席。
    """
    global _run_count
    _run_count += 1

    site_url = os.environ.get("SITE_URL", "https://USER.github.io/ai-research-radar")
    half_life = cfg["scoring"].get("time_decay", {}).get("half_life_hours", 4)
    w = cfg["runtime"].get("rolling_window_hours", 8)

    clustered_items: list[Item] = []
    new_events_list: list = []
    updated_events_list: list = []
    updated_events: dict = {}
    sit = None
    pipeline_ran = False

    # ================================================================
    # Stage 1-2: 采集 + 去重
    # ================================================================
    new_items = await collect_all(cfg)

    client = MinimaxClient(model=cfg["minimax"]["model"])
    try:
        if new_items:
            processor = Processor(client, cfg)
            processed = await processor.process(new_items)

            if processed:
                pipeline_ran = True

                # Stage 2.5: 交叉综合分析 —— 每轮必跑以最大化配额使用
                logger.info(f"Running cross-analysis on {len(processed)} items...")
                cross_analysis_text = await processor.cross_analyze(processed)
                if cross_analysis_text:
                    logger.info(f"Cross-analysis complete: {len(cross_analysis_text)} chars")
                else:
                    logger.warning("Cross-analysis returned empty")

                # Stage 2.6: 趋势发现 —— 每轮必跑以最大化配额使用
                logger.info("Running trend spotting on processed items...")
                trend_text = await processor.trend_spotting(processed)
                if trend_text:
                    logger.info(f"Trend spotting complete: {len(trend_text)} chars")
                else:
                    logger.warning("Trend spotting returned empty")

                # Stage 2.7: 视觉富化 —— 高分条目配图分析（图片理解 API，配额由 config 控制）
                logger.info("Running visual enrichment on high-score items...")
                await processor.visual_enrich(processed)
                visual_count = sum(1 for it in processed if it.visual_analysis)
                if visual_count:
                    logger.info(f"Visual enrich complete: {visual_count} items enriched")

                # Stage 2.8: 反向观点分析 —— 对高分条目提供替代解读
                logger.info("Running second opinion analysis...")
                await processor.second_opinion(processed)

                existing_events = load_events()
                cluster_engine = ClusterEngine(client, cfg)
                clustered_items, updated_events = await cluster_engine.cluster(
                    processed, existing_events
                )

                # Stage 2.9: 事件深度分析 —— 对新事件做多空逻辑、驱动因素分析
                deep_dive_eids = {it.event_id for it in clustered_items if it.is_new_event}
                if deep_dive_eids:
                    logger.info(f"Running event deep dive for {len(deep_dive_eids)} new events...")
                    for eid in deep_dive_eids:
                        event = updated_events.get(eid)
                        if not event:
                            continue
                        event_items = [it for it in clustered_items if it.event_id == eid]
                        analysis = await processor.event_deep_dive(event, event_items)
                        if analysis:
                            event.deep_analysis = analysis
                    deep_dive_count = sum(1 for e in updated_events.values() if e.deep_analysis)
                    logger.info(f"Event deep dive complete: {deep_dive_count} events analyzed")

                # 统计新事件
                new_event_count = sum(1 for it in clustered_items if it.is_new_event)
                updated_event_count = sum(1 for it in clustered_items if it.is_event_update)

                # ================================================================
                # Stage 5: 态势更新
                # ================================================================
                sit_gen = SituationGenerator(client, cfg)
                prev_sit = load_situation()

                new_event_ids = {it.event_id for it in clustered_items if it.is_new_event}
                updated_event_ids_set = {it.event_id for it in clustered_items if it.is_event_update}
                new_events_list = [updated_events[eid] for eid in new_event_ids if eid in updated_events]
                updated_events_list = [updated_events[eid] for eid in updated_event_ids_set if eid in updated_events]

                if sit_gen.should_update(prev_sit, _run_count, new_event_count):
                    sit = await sit_gen.generate(
                        updated_events, clustered_items, prev_sit
                    )
                    if cross_analysis_text and sit:
                        sit.cross_analysis = cross_analysis_text
                    if trend_text and sit:
                        sit.trend_spotting = trend_text
                    save_situation(sit)
                else:
                    sit = prev_sit
                    if cross_analysis_text and sit:
                        sit.cross_analysis = cross_analysis_text
                    if trend_text and sit:
                        sit.trend_spotting = trend_text
                    if (cross_analysis_text or trend_text) and sit:
                        from radar.models import utcnow_iso as _now
                        sit.generated_at = _now()
                        save_situation(sit)
                    logger.info("Skipping situation update (not due yet)")

                # ================================================================
                # Stage 6: 存储
                # ================================================================
                save_items(clustered_items)
                save_events(updated_events)

                logger.info(
                    f"Pipeline: {len(new_items)} new → "
                    f"{len(processed)} processed → "
                    f"{len(updated_events)} events "
                    f"(new: {new_event_count}, updated: {updated_event_count})"
                )
            else:
                logger.info("No items passed triage — rendering existing state")

            # 将所有抓取的条目标记为已见（即使未通过筛选），避免重复 LLM 调用
            dedup = DedupStore()
            dedup.mark_seen_batch([(it.id, it.title) for it in new_items])
            dedup.close()
        else:
            logger.info("No new items — rendering current state")

        # ================================================================
        # 三路汇合: 统一状态加载(管道未跑时从存储读取 + TTL 清理)
        # ================================================================
        if not pipeline_ran:
            updated_events = load_events()
            _reapply_event_ttl(updated_events, cfg["clustering"]["event_ttl_hours"])
            save_events(updated_events)
            sit = load_situation()

        # ================================================================
        # Stage 7: 分发 —— 报告的完整载体是 GitHub Issue + reports/ 归档,
        # 两者都在 notify 子系统内产出(实时看板/RSS/ticker 页已废弃删除)
        # ================================================================
        items_by_event: dict[str, list[Item]] = {}
        for it in clustered_items:
            if it.event_id:
                items_by_event.setdefault(it.event_id, []).append(it)
        from radar.notify.run import run as notify_run
        await notify_run(
            cfg,
            new_events=new_events_list,
            items_by_event=items_by_event,
            situation=sit,
            site_url=site_url,
            dry_run=notify_dry_run,
        )

    finally:
        await client.close()

    logger.info("Full pipeline done")


async def run_notify_only(cfg: dict, dry_run: bool = False) -> None:
    """仅运行推送子系统(不采集) —— 用于 dry-run 验证与手动触发"""
    site_url = os.environ.get("SITE_URL", "https://USER.github.io/ai-research-radar")
    sit = load_situation()
    from radar.notify.run import run as notify_run
    await notify_run(
        cfg,
        new_events=[],
        items_by_event={},
        situation=sit,
        site_url=site_url,
        dry_run=dry_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 投研雷达")
    parser.add_argument(
        "--stage",
        choices=["collect", "process", "cluster", "full", "notify"],
        default="collect",
        help="Pipeline stage to run",
    )
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument(
        "--notify-dry-run",
        action="store_true",
        help="新推送子系统 dry-run: 生成稿件打印到 stdout, 不发送、不写状态",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)

    logger.info(f"Starting radar pipeline [stage={args.stage}]")

    if args.stage == "collect":
        asyncio.run(run_collect(cfg))
    elif args.stage == "process":
        asyncio.run(run_process(cfg))
    elif args.stage == "cluster":
        asyncio.run(run_cluster(cfg))
    elif args.stage == "full":
        asyncio.run(run_full(cfg, notify_dry_run=args.notify_dry_run))
    elif args.stage == "notify":
        asyncio.run(run_notify_only(cfg, dry_run=args.notify_dry_run))

    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
