"""阅读流编排

两个入口, 分别挂在管道的不同位置:
- collect_candidates(): 每轮跑, 紧跟采集。条目只在被采集的那一轮可见, 错过就没了
- run_daily():          每轮判时点, 到点才出清单

时点模型沿用 notify.scheduler._check_morning: 目标时点 + 宽限窗口 + 日期去重键
+ 过窗补发, 容忍 Actions cron 抖动(实测间隔约 25 分钟, 不是 15)。
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from radar.minimax_client import MinimaxClient
from radar.models import Item, today_str
from radar.reading import fulltext, notes, render
from radar.reading.selector import select
from radar.reading.state import (
    load_reading_state, save_reading_state, enqueue, prune_stale,
)

logger = logging.getLogger(__name__)

_HKT = ZoneInfo("Asia/Hong_Kong")
_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"
_ISSUE_LABEL = "阅读"


def _now_hkt() -> datetime:
    return datetime.now(_HKT)


def is_due(cfg: dict, state: dict, now: Optional[datetime] = None) -> bool:
    """今天该不该出清单"""
    r_cfg = cfg.get("reading", {})
    if not r_cfg.get("enabled", False):
        return False
    if now is None:
        now = _now_hkt()
    today = now.strftime("%Y-%m-%d")
    if state.get("digest_last_date") == today:
        return False

    hour = r_cfg.get("hour_hkt", 9)
    before = r_cfg.get("window_before_min", 15)
    after = r_cfg.get("window_after_min", 90)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)

    if target - timedelta(minutes=before) <= now <= target + timedelta(minutes=after):
        return True
    # 过窗补发(容忍 Actions 排队抖动); 深夜不补 —— 凌晨三点出一份"今日值得读"没有意义
    return now > target + timedelta(minutes=after) and now.hour < 22


async def collect_candidates(items: list[Item], cfg: dict, client: MinimaxClient,
                             dry_run: bool = False) -> int:
    """对本轮采集到的条目跑阅读 rubric, 过闸的入队。返回新增条数"""
    if not cfg.get("reading", {}).get("enabled", False):
        return 0
    if not items:
        return 0

    candidates = await select(items, cfg, client)
    if not candidates:
        return 0

    if dry_run:
        logger.info(f"Reading [dry-run]: {len(candidates)} candidates would be queued")
        return 0

    state = load_reading_state()
    cap = cfg["reading"].get("queue_cap", 40)
    added = enqueue(state, candidates, cap)
    save_reading_state(state)
    logger.info(
        f"Reading: {added} candidates queued "
        f"({len(state['candidates'])} waiting for next digest)"
    )
    return added


async def run_daily(cfg: dict, client: MinimaxClient,
                    dry_run: bool = False, force: bool = False) -> Optional[str]:
    """到点则出一份每日清单。返回清单正文(未到点返回 None)"""
    if not cfg.get("reading", {}).get("enabled", False):
        return None

    state = load_reading_state()
    if not force and not is_due(cfg, state):
        return None

    r_cfg = cfg["reading"]
    today = today_str()
    limit = r_cfg.get("daily_limit", 5)

    # 一份清单覆盖「上一份清单到现在」这段区间 —— 队列由出刊消费, 不按日历切,
    # 否则 09:00 之后采到的东西会在午夜被静默丢掉
    prune_stale(state)
    ranked = sorted(state.get("candidates", []),
                    key=lambda c: c.get("score", 0), reverse=True)
    picked, skipped = ranked[:limit], ranked[limit:]

    logger.info(
        f"Reading digest {today}: {len(ranked)} candidates → "
        f"{len(picked)} picked, {len(skipped)} listed as skipped"
    )

    if picked:
        if r_cfg.get("fulltext", True):
            await fulltext.enrich(picked)
        await notes.write_notes(picked, client)

    body = render.render_digest(picked, skipped, today)

    if dry_run:
        print("\n" + "=" * 60)
        print(f"[DRY-RUN] reading digest {today}")
        print("=" * 60)
        print(body)
        return body

    issue_url = await _archive_issue(today, body)
    _write_report_file(today, body, len(picked), issue_url)

    # 出刊即清空: 入选的已经写进清单, 未入选的已在"未入选"里留档, 都不该再跟
    # 明天的新货竞争 —— 否则清单会越来越陈旧
    state["candidates"] = []
    state["digest_last_date"] = today
    save_reading_state(state)
    return body


async def _archive_issue(date_str: str, body: str) -> str:
    """当日一个 Issue; 已存在则覆盖正文(补发场景下不留旧版本)"""
    from radar.notify import transport

    title = f"今日值得读 · {date_str}"
    try:
        found = await transport.find_issue(title, label=_ISSUE_LABEL)
        if found:
            await transport.update_issue(found.get("number"), body)
            return found.get("html_url") or ""
        return await transport.create_issue(title, body, [_ISSUE_LABEL]) or ""
    except Exception as e:
        logger.warning(f"Reading issue archive failed: {e}")
        return ""


def _write_report_file(date_str: str, body: str, picked: int, issue_url: str) -> None:
    try:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        content = render.render_report_file(body, date_str, picked, issue_url)
        path = _REPORTS_DIR / f"reading-{date_str}.md"
        path.write_text(content, encoding="utf-8")
        logger.info(f"Reading digest archived: {path}")
    except OSError as e:
        logger.warning(f"Reading digest archive failed: {e}")
