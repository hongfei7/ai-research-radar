"""reading_queue.json 读写 —— 候选队列与出刊去重键

队列由「出刊」消费, 不按日历清空。这一点很关键: 清单在 09:00 出, 而采集全天在跑,
若按自然日清零, 09:00 之后采到的东西会在午夜被静默丢掉 —— 每天白扔十几个小时的采集。
改成出刊即清空后, 一份清单覆盖的是「上一份清单到现在」这段区间, 没有盲区。

age TTL 只是兜底: 万一出刊连续失败, 队列不至于无限堆积陈货。
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "reading_queue.json"

# 候选在队列里的最长寿命。取 48h: 正常情况下每天出刊会清空队列, 这条线只在
# 出刊连续失败时生效 —— 三天前的"今日值得读"已经没有意义了。
MAX_AGE_HOURS = 48

_DEFAULT = {
    "candidates": [],          # [{id,title,url,source,score,angle,why,queued_at,...}]
    "digest_last_date": "",    # 每日清单去重键 "2026-08-17"
}


def load_reading_state() -> dict:
    """加载阅读队列, 不存在或损坏时返回默认值"""
    if not _STATE_PATH.exists():
        return dict(_DEFAULT)
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = dict(_DEFAULT)
        # 只认默认字典里有的键 —— 与 notify.state 同一约定。新增字段务必先写进
        # _DEFAULT, 否则会在这一行被静默丢掉。
        state.update({k: v for k, v in data.items() if k in state})
        if not isinstance(state["candidates"], list):
            state["candidates"] = []
        return state
    except Exception as e:
        logger.warning(f"Failed to load reading_queue: {e}")
        return dict(_DEFAULT)


def save_reading_state(state: dict) -> Path:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved reading_queue to {_STATE_PATH}")
    return _STATE_PATH


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def prune_stale(state: dict, max_age_hours: int = MAX_AGE_HOURS,
                now: datetime | None = None) -> int:
    """丢掉在队列里待太久的候选, 返回丢弃条数"""
    if now is None:
        now = _utcnow()
    horizon = now - timedelta(hours=max_age_hours)
    kept = []
    for cand in state.get("candidates", []):
        try:
            queued = datetime.fromisoformat(
                str(cand.get("queued_at", "")).replace("Z", "+00:00"))
        except ValueError:
            # 没有可解析的入队时间就当它是新的 —— 宁可多留一轮, 不误杀
            kept.append(cand)
            continue
        if queued >= horizon:
            kept.append(cand)
    dropped = len(state.get("candidates", [])) - len(kept)
    state["candidates"] = kept
    if dropped:
        logger.info(f"Reading queue: pruned {dropped} stale candidates")
    return dropped


def enqueue(state: dict, candidates: list[dict], cap: int,
            now: datetime | None = None) -> int:
    """把新候选并入队列, 返回实际新增条数

    同 id 不重复入队(同一篇文章常被多个信源采到);
    超出 cap 时按分数保留高分 —— 溢出的是低分候选, 丢掉不可惜。
    队列的清空由出刊负责, 不在这里按日历切。
    """
    if now is None:
        now = _utcnow()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    state.setdefault("candidates", [])
    prune_stale(state, now=now)

    seen = {c.get("id") for c in state["candidates"]}
    added = 0
    for cand in candidates:
        cid = cand.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        state["candidates"].append({**cand, "queued_at": stamp})
        added += 1

    if len(state["candidates"]) > cap:
        state["candidates"].sort(key=lambda c: c.get("score", 0), reverse=True)
        dropped = len(state["candidates"]) - cap
        state["candidates"] = state["candidates"][:cap]
        logger.info(f"Reading queue over cap: dropped {dropped} lowest-scoring candidates")

    return added
