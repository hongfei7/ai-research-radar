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
from radar.render import render_daily_brief
from radar.publish import (
    create_daily_issue, send_telegram, format_telegram_alert,
    update_readme, should_telegram_alert,
    send_wecom_markdown, send_wecom_news, send_wecom_brief,
    format_wecom_alert, should_wecom_alert,
)

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

    all_sources = []
    for src_type in ["tech", "market"]:
        for src in cfg["sources"].get(src_type, []):
            all_sources.append((src["id"], src["type"], src.get("params", {})))

    logger.info(f"Collecting from {len(all_sources)} sources (parallel)...")

    # 并发采集所有信源
    async def _fetch_one(src_id, src_type, params):
        collector = COLLECTOR_MAP.get(src_type)
        if collector is None:
            logger.warning(f"No collector for type '{src_type}' (source: {src_id}), skipping")
            return []
        try:
            return await collector.fetch(src_id, params)
        except Exception as e:
            logger.error(f"[{src_id}] Collector failed: {e}")
            return []

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

    # —— 时间窗口过滤：只保留最近 N 小时内发布的内容 ——
    window_hours = cfg["runtime"].get("rolling_window_hours", 8)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    filtered_items = []
    stale_count = 0
    no_date_count = 0

    for it in all_items:
        pub_dt = get_effective_date(it)
        if pub_dt is None:
            # published_at 不可解析：保留但标记（不 fallback 到 fetched_at）
            no_date_count += 1
            filtered_items.append(it)
            continue

        if pub_dt >= cutoff:
            filtered_items.append(it)
        else:
            stale_count += 1

    if stale_count > 0:
        logger.info(
            f"Time filter: {stale_count} items older than {window_hours}h removed, "
            f"{no_date_count} without date kept, {len(filtered_items)} remaining"
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


async def _wecom_fallback_push(sit, today_events: dict, site_url: str, cfg: dict) -> None:
    """企业微信兜底推送：无新条目时按间隔推送当前态势"""
    wx_cfg = cfg.get("channels", {}).get("wecom", {})
    if not wx_cfg.get("enabled", False) or not sit:
        return
    try:
        if should_wecom_alert([], [], sit, cfg):
            all_ev = list(today_events.values())
            max_n = wx_cfg.get("notify_max_new_events", 6)
            card = format_wecom_alert(
                new_events=[], updated_events=[],
                all_active_events=all_ev, situation=sit, site_url=site_url,
                max_new_events=max_n,
            )
            if await send_wecom_markdown(card):
                from radar.models import utcnow_iso
                sit.last_wecom_digest_at = utcnow_iso()
                save_situation(sit)
    except Exception as e:
        logger.error(f"WeCom fallback push failed: {e}")


async def _legacy_distribute(
    cfg: dict,
    clustered_items: list[Item],
    new_events_list: list,
    updated_events_list: list,
    updated_events: dict,
    sit,
    site_url: str,
) -> None:
    """旧分发路径(灰度期保留, notify.use_legacy=true 时使用, S8 删除)"""
    channels = cfg.get("channels", {})

    # GitHub Issue + 晨报 Telegram 推送（仅日报时间）
    issue_cfg = channels.get("github_issue", {})
    tg_cfg = channels.get("telegram", {})
    if issue_cfg.get("enabled", False):
        try:
            from zoneinfo import ZoneInfo
            hkt_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
            schedule_hour = issue_cfg.get("schedule_hour_hkt", 7)
            # 在目标小时 ±1h 且前半小时内触发
            if abs(hkt_now.hour - schedule_hour) <= 1 and hkt_now.minute < 30:
                today_str_hkt = hkt_now.strftime("%Y-%m-%d")
                if sit and sit.morning_brief_date != today_str_hkt:
                    synthesis = sit.text if sit else ""
                    brief_md = render_daily_brief(clustered_items, synthesis, site_url, cfg=cfg)
                    # 日报用更长的窗口（24h）
                    issue_url = await create_daily_issue(
                        brief_md, issue_cfg.get("label", "晨报")
                    )
                    update_readme(issue_url, site_url)

                    # 同时推送晨报全文到 Telegram（每天只推一次）
                    from radar.publish import send_telegram as _send_tg
                    tg_brief = f"*AI 投研雷达 · 晨报 · {today_str_hkt}*\n\n{brief_md}"
                    if len(tg_brief) > 4000:
                        tg_brief = tg_brief[:3950] + "\n\n[...完整版见 Issue]"
                    await _send_tg(tg_brief, parse_mode="Markdown")

                    # 同时推送晨报到企业微信
                    if channels.get("wecom", {}).get("enabled", False):
                        wx_title = f"AI 投研雷达 · 晨报 · {today_str_hkt}"
                        await send_wecom_brief(wx_title, brief_md, issue_url, site_url)

                    sit.morning_brief_date = today_str_hkt
                    save_situation(sit)
        except Exception as e:
            logger.error(f"Daily brief / issue failed: {e}")

    # Telegram 智能推送
    if tg_cfg.get("enabled", False):
        try:
            if should_telegram_alert(
                new_events_list, updated_events_list, sit, cfg
            ):
                # 本轮新增的条目（is_new_event 或 is_event_update）
                new_items_this_run = [
                    it for it in clustered_items
                    if it.is_new_event or it.is_event_update
                ]
                all_events_list = list(updated_events.values())
                alert_text = format_telegram_alert(
                    new_events=new_events_list,
                    updated_events=updated_events_list,
                    all_active_events=all_events_list,
                    new_items=new_items_this_run,
                    situation=sit,
                    site_url=site_url,
                )
                if await send_telegram(alert_text):
                    # 仅推送成功才更新兜底推送时间
                    if sit:
                        from radar.models import utcnow_iso
                        sit.last_telegram_digest_at = utcnow_iso()
                        save_situation(sit)
        except Exception as e:
            logger.error(f"Telegram push failed: {e}")

    # 企业微信智能推送（群机器人 Webhook）
    wx_cfg = channels.get("wecom", {})
    if wx_cfg.get("enabled", False):
        try:
            if should_wecom_alert(
                new_events_list, updated_events_list, sit, cfg
            ):
                all_events_list = list(updated_events.values())
                max_n = wx_cfg.get("notify_max_new_events", 6)
                card = format_wecom_alert(
                    new_events=new_events_list,
                    updated_events=updated_events_list,
                    all_active_events=all_events_list,
                    situation=sit,
                    site_url=site_url,
                    items=clustered_items,
                    max_new_events=max_n,
                )
                if await send_wecom_markdown(card):
                    if sit:
                        from radar.models import utcnow_iso
                        sit.last_wecom_digest_at = utcnow_iso()
                        save_situation(sit)
        except Exception as e:
            logger.error(f"WeCom push failed: {e}")


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
        # Stage 7: 渲染 —— 实时看板/RSS/ticker 页已废弃删除,
        # 报告的唯一完整载体为 GitHub Issue(notify 子系统内创建)
        # ================================================================

        # ================================================================
        # Stage 8: 分发(新 notify 子系统 / 旧路径由 notify.use_legacy 切换)
        # ================================================================
        notify_cfg = cfg.get("notify", {})
        use_legacy = notify_cfg.get("use_legacy", True) or not notify_cfg.get("enabled", False)

        if not use_legacy:
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
        elif pipeline_ran:
            await _legacy_distribute(
                cfg, clustered_items, new_events_list, updated_events_list,
                updated_events, sit, site_url,
            )
        else:
            await _wecom_fallback_push(sit, updated_events, site_url, cfg)

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
