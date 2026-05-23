"""归档与状态存储"""

import json
import logging
from pathlib import Path
from typing import Optional

from radar.models import Item, Event, Situation, today_str

logger = logging.getLogger(__name__)

_ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "archive"
_STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def _ensure_dirs() -> None:
    _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def save_items(items: list[Item], date_str: str | None = None) -> Path:
    """保存当日处理结果到 JSONL，幂等（同 id 覆盖）"""
    _ensure_dirs()
    if date_str is None:
        date_str = today_str()
    path = _ARCHIVE_DIR / f"{date_str}.jsonl"

    # 幂等：先读已有条目，用 dict 实现同 id 覆盖
    existing: dict[str, dict] = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                        existing[obj["id"]] = obj
                    except json.JSONDecodeError:
                        continue

    for item in items:
        existing[item.id] = item.to_dict()

    with open(path, "w", encoding="utf-8") as f:
        for obj in existing.values():
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    logger.info(f"Saved {len(existing)} items to {path}")
    return path


def load_items(date_str: str | None = None) -> list[Item]:
    """读取指定日期的 JSONL"""
    _ensure_dirs()
    if date_str is None:
        date_str = today_str()
    path = _ARCHIVE_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []

    items: list[Item] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(Item.from_dict(json.loads(line)))
                except Exception as e:
                    logger.warning(f"Failed to parse item: {e}")
    return items


def save_events(events: dict[str, Event]) -> Path:
    """保存事件聚类状态到 state/events.json"""
    _ensure_dirs()
    path = _STATE_DIR / "events.json"
    data = {
        "updated_at": _now_iso(),
        "events": {eid: ev.to_dict() for eid, ev in events.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(events)} events to {path}")
    return path


def load_events() -> dict[str, Event]:
    """从 state/events.json 加载事件状态"""
    _ensure_dirs()
    path = _STATE_DIR / "events.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {eid: Event.from_dict(ev) for eid, ev in data.get("events", {}).items()}
    except Exception as e:
        logger.warning(f"Failed to load events: {e}")
        return {}


def save_situation(sit: Situation) -> Path:
    """保存当前态势到 state/situation.json"""
    _ensure_dirs()
    path = _STATE_DIR / "situation.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sit.to_dict(), f, ensure_ascii=False, indent=2)
    logger.info(f"Saved situation to {path}")
    return path


def load_situation() -> Optional[Situation]:
    """从 state/situation.json 加载态势"""
    _ensure_dirs()
    path = _STATE_DIR / "situation.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Situation.from_dict(data)
    except Exception as e:
        logger.warning(f"Failed to load situation: {e}")
        return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
