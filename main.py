#!/usr/bin/env python3
"""AI 投研雷达 — 管道入口

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
from pathlib import Path

from radar.config import load_config
from radar.models import Item, today_str
from radar.collectors.rss import RSSCollector
from radar.collectors.arxiv import ArxivCollector
from radar.collectors.hackernews import HackerNewsCollector
from radar.collectors.github_trending import GithubTrendingCollector
from radar.collectors.sec_edgar import SECEdgarCollector
from radar.dedup import DedupStore
from radar.minimax_client import MinimaxClient
from radar.processor import Processor
from radar.cluster import ClusterEngine
from radar.situation import SituationGenerator
from radar.storage import save_items, load_events, save_events, load_situation, save_situation
from radar.render import (
    write_rss, write_dashboard, write_ticker_pages,
    write_daily_brief, render_daily_brief,
)
from radar.publish import (
    create_daily_issue, send_telegram, format_telegram_alert,
    update_readme, should_telegram_alert,
)

logger = logging.getLogger("radar")

COLLECTOR_MAP = {
    "rss": RSSCollector(),
    "arxiv": ArxivCollector(),
    "hackernews": HackerNewsCollector(),
    "github_trending": GithubTrendingCollector(),
    "sec_edgar": SECEdgarCollector(),
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
    dedup = DedupStore()

    # 注入 coverage 到 SEC EDGAR 采集器
    sec_collector = COLLECTOR_MAP.get("sec_edgar")
    if isinstance(sec_collector, SECEdgarCollector):
        sec_collector.set_coverage(cfg.get("coverage", []))

    all_sources = []
    for src_type in ["tech", "market"]:
        for src in cfg["sources"].get(src_type, []):
            all_sources.append((src["id"], src["type"], src.get("params", {})))

    logger.info(f"Collecting from {len(all_sources)} sources...")

    all_items = []
    for src_id, src_type, params in all_sources:
        collector = COLLECTOR_MAP.get(src_type)
        if collector is None:
            logger.warning(f"No collector for type '{src_type}' (source: {src_id}), skipping")
            continue
        try:
            items = await collector.fetch(src_id, params)
            all_items.extend(items)
        except Exception as e:
            logger.error(f"[{src_id}] Collector failed: {e}", exc_info=True)
            continue

    logger.info(f"Collected {len(all_items)} raw items total")

    new_items: list[Item] = []
    if all_items:
        all_ids = [it.id for it in all_items]
        new_ids = dedup.filter_new(all_ids)
        id_set = set(new_ids)
        new_items = [it for it in all_items if it.id in id_set]
        # 立即标记所有采集到的条目为 seen，防止管道中途崩溃导致下次重复处理
        if all_items:
            dedup.mark_seen_batch([(it.id, it.title) for it in all_items])
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
        dedup = DedupStore()
        dedup.mark_seen_batch([(it.id, it.title) for it in processed])
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

        dedup = DedupStore()
        dedup.mark_seen_batch([(it.id, it.title) for it in clustered_items])
        dedup.close()

        logger.info(
            f"M3 cluster stage done: {len(new_items)} new → "
            f"{len(processed)} processed → {len(updated_events)} events"
        )
        return clustered_items, {"events": updated_events}

    finally:
        await client.close()


async def run_full(cfg: dict) -> None:
    """M4+: 完整管道 → 采集+处理+聚类+态势+渲染+分发"""
    global _run_count
    _run_count += 1

    site_url = os.environ.get("SITE_URL", "https://USER.github.io/ai-research-radar")

    # ================================================================
    # Stage 1-2: 采集 + 去重
    # ================================================================
    new_items = await collect_all(cfg)
    if not new_items:
        logger.info("No new items — skipping processing, still rendering current state")

        # 即使没有新条目，也渲染当前状态（读已有数据）
        today_items = []
        today_events = load_events()
        sit = load_situation()

        all_events_list = sorted(
            today_events.values(), key=lambda e: e.significance, reverse=True
        )
        active_events = [e for e in all_events_list if e.is_active]

        write_rss(today_items, site_url, cfg["channels"]["rss"].get("max_items", 50))
        write_dashboard(today_items, active_events, sit, site_url)
        return

    # ================================================================
    # Stage 3-4: MiniMax 处理 + 事件聚类
    # ================================================================
    client = MinimaxClient(model=cfg["minimax"]["model"])
    try:
        processor = Processor(client, cfg)
        processed = await processor.process(new_items)

        if not processed:
            await client.close()
            return

        existing_events = load_events()
        cluster_engine = ClusterEngine(client, cfg)
        clustered_items, updated_events = await cluster_engine.cluster(
            processed, existing_events
        )

        # 统计新事件
        new_event_count = sum(1 for it in clustered_items if it.is_new_event)
        updated_event_count = sum(1 for it in clustered_items if it.is_event_update)

        # ================================================================
        # Stage 5: 态势更新
        # ================================================================
        sit_gen = SituationGenerator(client, cfg)
        prev_sit = load_situation()

        new_events_list = [
            updated_events[eid]
            for eid in updated_events
            if eid in [it.event_id for it in clustered_items if it.is_new_event]
        ]
        updated_events_list = [
            updated_events[eid]
            for eid in updated_events
            if eid in [it.event_id for it in clustered_items if it.is_event_update]
        ]

        if sit_gen.should_update(prev_sit, _run_count, new_event_count):
            sit = await sit_gen.generate(
                updated_events, clustered_items, prev_sit
            )
            save_situation(sit)
        else:
            sit = prev_sit
            logger.info("Skipping situation update (not due yet)")

        # ================================================================
        # Stage 6: 存储
        # ================================================================
        save_items(clustered_items)
        save_events(updated_events)

        dedup = DedupStore()
        dedup.mark_seen_batch([(it.id, it.title) for it in clustered_items])
        dedup.close()

        # ================================================================
        # Stage 7: 渲染
        # ================================================================
        rss_config = cfg["channels"]["rss"]
        write_rss(clustered_items, site_url, rss_config.get("max_items", 50))

        all_events_sorted = sorted(
            updated_events.values(), key=lambda e: e.significance, reverse=True
        )
        active_events = [e for e in all_events_sorted if e.is_active]

        write_dashboard(clustered_items, active_events, sit, site_url)
        write_ticker_pages(clustered_items, active_events, site_url)

        # ================================================================
        # Stage 8: 分发
        # ================================================================
        channels = cfg.get("channels", {})

        # GitHub Issue + 晨报 Telegram 推送（仅日报时间）
        issue_cfg = channels.get("github_issue", {})
        tg_cfg = channels.get("telegram", {})
        if issue_cfg.get("enabled", False):
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                hkt_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
                schedule_hour = issue_cfg.get("schedule_hour_hkt", 7)
                # 在目标小时 ±1h 且前半小时内触发
                if abs(hkt_now.hour - schedule_hour) <= 1 and hkt_now.minute < 30:
                    synthesis = sit.text if sit else ""
                    brief_md = render_daily_brief(clustered_items, synthesis, site_url)
                    issue_url = await create_daily_issue(
                        brief_md, issue_cfg.get("label", "晨报")
                    )
                    update_readme(issue_url, site_url)

                    # 同时推送晨报全文到 Telegram（每天只推一次）
                    today_str_hkt = hkt_now.strftime("%Y-%m-%d")
                    if sit and sit.morning_brief_date != today_str_hkt:
                        from radar.publish import send_telegram as _send_tg
                        tg_brief = f"*AI 投研雷达 · 晨报 · {today_str_hkt}*\n\n{brief_md}"
                        # Telegram Markdown 对某些字符敏感，用 MarkdownV2 或截断处理
                        if len(tg_brief) > 4000:
                            tg_brief = tg_brief[:3950] + "\n\n[...完整版见 Issue]"
                        await _send_tg(tg_brief, parse_mode="Markdown")
                        sit.morning_brief_date = today_str_hkt
                        save_situation(sit)
            except Exception as e:
                logger.error(f"Daily brief / issue failed: {e}")

        # Telegram 智能推送
        tg_cfg = channels.get("telegram", {})
        if tg_cfg.get("enabled", False):
            try:
                if should_telegram_alert(
                    new_events_list, updated_events_list, sit, cfg
                ):
                    alert_text = format_telegram_alert(
                        list(updated_events.values()), sit, new_event_count
                    )
                    # 加链接
                    alert_text += f"\n\n[实时看板]({site_url})"
                    await send_telegram(alert_text)
                    # 更新兜底推送时间
                    if sit:
                        from radar.models import utcnow_iso
                        sit.last_telegram_digest_at = utcnow_iso()
                        save_situation(sit)
            except Exception as e:
                logger.error(f"Telegram push failed: {e}")

    finally:
        await client.close()

    logger.info(
        f"Full pipeline done: {len(new_items)} new → "
        f"{len(processed)} processed → "
        f"{len(updated_events)} events "
        f"(new: {new_event_count}, updated: {updated_event_count})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 投研雷达")
    parser.add_argument(
        "--stage",
        choices=["collect", "process", "cluster", "full"],
        default="collect",
        help="Pipeline stage to run",
    )
    parser.add_argument("--config", type=str, help="Path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
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
        asyncio.run(run_full(cfg))

    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
