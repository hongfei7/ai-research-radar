"""推送调度 —— 决定本轮该发哪些产品

时点模型(审计 H1): 目标时点 + 宽限期 + 去重键, 容忍 Actions cron 5-30min 抖动
- 每日内参: 07:00 HKT, 窗口 [06:45, 08:00], 过窗未发 → 当日补发
- 周末复盘: 周日 20:00 HKT(可配), 按 ISO 周去重
- 首席快报: 即时, sig≥8, 每轮 ≤3 条, 溢出并入下一轮快报评估(审计 F4)
- 静默(HKT 01:00-07:00): 仅快报 sig≥9 可破例; 内参/复盘不受约束(审计 M3)

快讯防重(审计 S3+F9): 内容签名 = 主 ticker 集合 + 首条原始条目标题 bigram,
冷却期内 Jaccard≥阈值判重(不用 event_id —— 6h TTL 重建后不稳定;
不用 event.title —— LLM 重写会漂移)。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from radar.cluster import _tokenize_for_matching
from radar.models import Event, Item, parse_iso

logger = logging.getLogger(__name__)

_HKT = ZoneInfo("Asia/Hong_Kong")


@dataclass
class PushTask:
    """一次推送任务"""

    kind: str                                  # morning | digest | breaking
    slot_key: str = ""                         # 去重键(日期/槽位)
    events: list = field(default_factory=list) # breaking: [Event]
    items_by_event: dict = field(default_factory=dict)  # breaking: eid → [Item]
    reason: str = ""


def _now_hkt() -> datetime:
    return datetime.now(_HKT)


def _in_quiet_hours(now: datetime) -> bool:
    return 1 <= now.hour < 7


# ================================================================
# 快讯内容签名指纹
# ================================================================

def content_fingerprint(ev: Event, items: list[Item]) -> dict:
    """内容签名: 主 ticker 集合 + 首条原始条目标题特征

    用 item 级原始标题(稳定), 不用 event 级 LLM 标题(_rewrite_event 会漂移)。
    """
    # 首条原始条目: 取最早发布的
    first_title = ""
    if items:
        def _key(it: Item) -> str:
            return it.published_at or "9999"
        first_title = sorted(items, key=_key)[0].title
    if not first_title:
        first_title = ev.title or ""
    return {
        "tickers": sorted((ev.tickers or [])[:3]),
        "features": sorted(_tokenize_for_matching(first_title)),
        "sent_at": "",  # 由调用方填充
    }


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_duplicate_fingerprint(
    fp: dict,
    existing: list[dict],
    cooldown_min: int,
    jaccard_threshold: float,
    now: Optional[datetime] = None,
) -> bool:
    """冷却期内特征 Jaccard≥阈值 且 ticker 有交集 → 同一突发"""
    from datetime import timezone
    if now is None:
        now = datetime.now(timezone.utc)
    for old in existing:
        try:
            sent = parse_iso(old.get("sent_at"))
            if sent is None:
                continue
            if (now - sent).total_seconds() > cooldown_min * 60:
                continue
        except Exception:
            continue
        feat_sim = _jaccard(set(fp.get("features", [])), set(old.get("features", [])))
        tk_overlap = bool(set(fp.get("tickers", [])) & set(old.get("tickers", [])))
        if feat_sim >= jaccard_threshold and tk_overlap:
            return True
    return False


# ================================================================
# 各产品判定
# ================================================================

def _check_morning(cfg: dict, state: dict, now: datetime) -> Optional[PushTask]:
    m_cfg = cfg.get("notify", {}).get("morning", {})
    if not m_cfg.get("enabled", True):
        return None
    today = now.strftime("%Y-%m-%d")
    if state.get("morning_last_date") == today:
        return None

    hour = m_cfg.get("hour_hkt", 7)
    before = m_cfg.get("window_before_min", 15)
    after = m_cfg.get("window_after_min", 60)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    window_start = target - timedelta(minutes=before)
    window_end = target + timedelta(minutes=after)

    if window_start <= now <= window_end:
        return PushTask(kind="morning", slot_key=today, reason="晨报窗口")
    if now > window_end and now.hour < 22:
        # 过窗补发(容忍 Actions 排队抖动); 深夜不补
        return PushTask(kind="morning", slot_key=today, reason="晨报补发(延迟)")
    return None


def _check_weekly(cfg: dict, state: dict, now: datetime) -> Optional[PushTask]:
    """周末复盘: 默认周日 20:00 HKT, 窗口 [19:45, 当日 23:59], 按 ISO 周去重"""
    w_cfg = cfg.get("notify", {}).get("weekly", {})
    if not w_cfg.get("enabled", True):
        return None
    weekday = w_cfg.get("weekday", 6)  # 0=周一 ... 6=周日
    if now.weekday() != weekday:
        return None
    hour = w_cfg.get("hour_hkt", 20)
    before = w_cfg.get("window_before_min", 15)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < target - timedelta(minutes=before):
        return None
    iso_year, iso_week, _ = now.isocalendar()
    slot_key = f"{iso_year}-W{iso_week:02d}"
    if state.get("weekly_last_slot") == slot_key:
        return None
    return PushTask(kind="weekly", slot_key=slot_key, reason="周末复盘窗口")


def _content_is_fresh(ev: Event, items_by_event: dict, max_age_hours: float) -> bool:
    """事件最新成员条目是否在年龄上限内

    采集层堵住无日期后普通条目天然新鲜, 但批量发布源的窗口是放宽的
    (SEC 72h / arXiv 30h), 三天前的 8-K 不该以"突发"的名义推送。
    取不到发布时间时按不通过处理 —— 拿不出新鲜度证据就不推。
    """
    from datetime import timezone
    published = [
        dt for dt in (parse_iso(it.published_at)
                      for it in items_by_event.get(ev.event_id, []))
        if dt is not None
    ]
    if not published:
        return False
    age_h = (datetime.now(timezone.utc) - max(published)).total_seconds() / 3600
    return age_h <= max_age_hours


def _check_breaking(cfg: dict, state: dict, now: datetime,
                    new_events: list[Event],
                    items_by_event: dict) -> Optional[PushTask]:
    b_cfg = cfg.get("notify", {}).get("breaking", {})
    if not b_cfg.get("enabled", True):
        return None
    if not new_events:
        return None

    min_sig = b_cfg.get("min_significance", 8)
    breakthrough_sig = b_cfg.get("quiet_hours_breakthrough", 9)
    cooldown = b_cfg.get("cooldown_min", 30)
    jaccard = b_cfg.get("jaccard_threshold", 0.6)
    max_per_run = b_cfg.get("max_per_run", 3)

    quiet = _in_quiet_hours(now)
    threshold = breakthrough_sig if quiet else min_sig
    max_age_h = b_cfg.get("max_content_age_hours", 24)

    candidates = []
    for ev in new_events:
        if ev.significance < threshold:
            continue
        if not _content_is_fresh(ev, items_by_event, max_age_h):
            logger.info(f"Breaking skipped (stale content): {ev.title[:40]}")
            continue
        candidates.append(ev)
    candidates.sort(key=lambda e: e.significance, reverse=True)

    existing_fps = state.get("breaking_fingerprints", [])
    selected = []
    for ev in candidates:
        if len(selected) >= max_per_run:
            break  # 溢出并入下一轮快讯评估(审计 F4)
        items = items_by_event.get(ev.event_id, [])
        fp = content_fingerprint(ev, items)
        if is_duplicate_fingerprint(fp, existing_fps, cooldown, jaccard):
            logger.info(f"Breaking dedup: {ev.title[:30]} matches recent fingerprint")
            continue
        selected.append(ev)

    if not selected:
        return None
    return PushTask(
        kind="breaking",
        events=selected,
        items_by_event=items_by_event,
        reason=f"{len(selected)} 个 sig≥{threshold} 新事件",
    )


def decide(
    cfg: dict,
    state: dict,
    new_events: list[Event],
    items_by_event: dict,
    now: Optional[datetime] = None,
) -> list[PushTask]:
    """返回本轮应执行的推送任务, 按优先级排序: 快报 > 内参 > 复盘"""
    if now is None:
        now = _now_hkt()
    tasks: list[PushTask] = []

    breaking = _check_breaking(cfg, state, now, new_events, items_by_event)
    if breaking:
        tasks.append(breaking)

    morning = _check_morning(cfg, state, now)
    if morning:
        tasks.append(morning)

    weekly = _check_weekly(cfg, state, now)
    if weekly:
        tasks.append(weekly)

    if tasks:
        logger.info(f"Notify decide: {[(t.kind, t.reason) for t in tasks]}")
    return tasks
