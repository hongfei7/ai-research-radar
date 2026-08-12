"""notify_state.json 读写 —— 推送去重键与快讯指纹

与 Situation 解耦(审计 H2): 推送状态独立存放, 灰度期 Situation 旧字段双写保留。
at-least-once 语义: state 丢失可能重发, 由强去重键(日期键/内容指纹)降低概率。
"""

import json
import logging
from pathlib import Path

from radar.models import utcnow_iso

logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "notify_state.json"

_DEFAULT = {
    "morning_last_date": "",       # 每日内参当日去重键 "2026-08-12"
    "weekly_last_slot": "",        # 周末复盘周去重键 "2026-W32"
    "breaking_fingerprints": [],   # 快报指纹 [{tickers, features, sent_at}]
}


def load_notify_state() -> dict:
    """加载推送状态, 不存在或损坏时返回默认值"""
    if not _STATE_PATH.exists():
        return dict(_DEFAULT)
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = dict(_DEFAULT)
        state.update({k: v for k, v in data.items() if k in state})
        if not isinstance(state["breaking_fingerprints"], list):
            state["breaking_fingerprints"] = []
        return state
    except Exception as e:
        logger.warning(f"Failed to load notify_state: {e}")
        return dict(_DEFAULT)


def save_notify_state(state: dict) -> Path:
    """保存推送状态"""
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved notify_state to {_STATE_PATH}")
    return _STATE_PATH


def prune_fingerprints(state: dict, cooldown_min: int, now_iso: str | None = None) -> dict:
    """清理冷却期外的快讯指纹(只保留最近 24h, 防止无限增长)"""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    keep_horizon = now - timedelta(hours=24)
    kept = []
    for fp in state.get("breaking_fingerprints", []):
        try:
            sent = datetime.fromisoformat(str(fp.get("sent_at", "")).replace("Z", "+00:00"))
            if sent >= keep_horizon:
                kept.append(fp)
        except Exception:
            continue
    state["breaking_fingerprints"] = kept
    return state
