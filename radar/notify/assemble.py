"""数据装配层 —— 按时间窗从 events.json + archive/*.jsonl 加载撰稿素材

修复现存 bug(审计 S2): 旧晨报只收到触发轮 15 分钟的条目(main.py 旧 Stage 8),
本层独立按时间窗装配, 不依赖"本轮 clustered_items"。

token 预算: 输入素材控制在 ~16000 字符(≈8k tokens 中英混合),
按 significance 降序裁剪; 风险素材(反向观点/低可信条目)单独配额,
不进入 sig 裁剪池(审计 R2 —— 否则晨报永远写不出"风险与分歧")。
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from radar.models import Item, Event, Situation, parse_iso, get_event_effective_date
from radar.storage import load_events, load_items, load_situation

logger = logging.getLogger(__name__)

# 素材总字符预算(粗略 2 chars ≈ 1 token)
_MAX_MATERIAL_CHARS = 16000
# 单个字段裁剪
_EVENT_SUMMARY_CLIP = 220
_EVENT_ANALYSIS_CLIP = 300
_ITEM_SUMMARY_CLIP = 150
# 风险素材配额(字符), 与主素材分开计算
_RISK_QUOTA_CHARS = 2000


def _clip(text: str, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def _window_cutoff(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def load_window_events(hours: float) -> list[Event]:
    """加载最近 N 小时内有更新的事件(活跃 + 近期 resolved), 按重要性降序"""
    events = load_events()
    cutoff = _window_cutoff(hours)
    result = []
    for ev in events.values():
        dt = get_event_effective_date(ev)
        if dt is None or dt >= cutoff:
            result.append(ev)
    result.sort(key=lambda e: (e.significance, e.last_updated_at or ""), reverse=True)
    return result


def load_window_items(hours: float) -> list[Item]:
    """从 archive 跨日加载最近 N 小时发布的条目(按发布时间过滤)"""
    cutoff = _window_cutoff(hours)
    now = datetime.now(timezone.utc)
    items: dict[str, Item] = {}
    # 窗口最长 24h, 最多横跨 2 个 archive 文件(按 HKT 日期命名)
    from radar.models import today_str
    days = 2 if hours > 12 else 1
    for d in range(days):
        date_str = today_str() if d == 0 else (
            datetime.now(timezone.utc) + timedelta(hours=8) - timedelta(days=d)
        ).strftime("%Y-%m-%d")
        for it in load_items(date_str):
            items[it.id] = it
    result = []
    for it in items.values():
        dt = parse_iso(it.published_at)
        if dt is None or dt >= cutoff:
            result.append(it)
    result.sort(key=lambda x: x.relevance_score or 0, reverse=True)
    return result


def _event_to_material(ev: Event) -> dict:
    return {
        "event_id": ev.event_id,
        "title": ev.title,
        "summary": _clip(ev.summary, _EVENT_SUMMARY_CLIP),
        "tickers": (ev.tickers or [])[:8],
        "themes": ev.themes or [],
        "direction": ev.direction or {},
        "significance": ev.significance,
        "status": ev.status,
        "is_active": ev.is_active,
        "source_count": ev.source_count,
        "last_updated_at": ev.last_updated_at,
        "deep_analysis": _clip(ev.deep_analysis, _EVENT_ANALYSIS_CLIP),
    }


def _item_to_material(it: Item) -> dict:
    return {
        "title": it.title,
        "tickers": (it.tickers or [])[:6],
        "direction": it.direction or {},
        "relevance_score": it.relevance_score,
        "cn_summary": _clip(it.cn_summary, _ITEM_SUMMARY_CLIP),
        "so_what": _clip(it.so_what, 120),
        "credibility": it.credibility,
        "source": it.source,
        "published_at": it.published_at,
        "url": it.url,
    }


def _json_chars(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def assemble_material(
    hours: float,
    situation: Optional[Situation] = None,
    themes_map: Optional[dict] = None,
) -> dict:
    """装配撰稿素材

    Returns:
        {
          "window_hours": ..., "generated_at": ...,
          "situation": ..., "trend_spotting": ..., "cross_analysis": ...,
          "events": [...],        # 按预算裁剪
          "risk_material": [...], # 反向观点/低可信, 单独配额
          "dropped_events": N,    # 被预算裁掉的事件数(透明化)
        }
    """
    if situation is None:
        situation = load_situation()
    events = load_window_events(hours)

    material: dict = {
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "situation": _clip(situation.text, 400) if situation else "",
        "trend_spotting": _clip(situation.trend_spotting, 500) if situation else "",
        "cross_analysis": _clip(situation.cross_analysis, 600) if situation else "",
        "key_themes": situation.key_themes if situation else [],
        "themes_map": themes_map or {},
        "events": [],
        "risk_material": [],
        "dropped_events": 0,
    }

    base_chars = _json_chars({k: v for k, v in material.items() if k != "events"})
    budget = _MAX_MATERIAL_CHARS - base_chars

    # 主素材: 按 significance 降序填入, 超预算即截断
    used = 0
    for ev in events:
        entry = _event_to_material(ev)
        cost = _json_chars(entry)
        if used + cost > budget:
            material["dropped_events"] += 1
            continue
        material["events"].append(entry)
        used += cost

    # 风险素材: 反向观点 + 低可信条目, 单独配额(不参与 sig 裁剪)
    risk_used = 0
    try:
        items = load_window_items(hours)
    except Exception as e:
        logger.warning(f"assemble: load items failed: {e}")
        items = []
    for it in items:
        if risk_used >= _RISK_QUOTA_CHARS:
            break
        opinion = (it.second_opinion or "").strip()
        low_cred = it.credibility in ("low", "🔴", "🔴 low")
        if not opinion and not low_cred:
            continue
        entry = {
            "title": it.title,
            "tickers": (it.tickers or [])[:4],
            "second_opinion": _clip(opinion, 200),
            "credibility": it.credibility,
            "relevance_score": it.relevance_score,
        }
        cost = _json_chars(entry)
        if risk_used + cost > _RISK_QUOTA_CHARS:
            break
        material["risk_material"].append(entry)
        risk_used += cost

    logger.info(
        f"Assembled material: {len(material['events'])} events "
        f"({material['dropped_events']} dropped), "
        f"{len(material['risk_material'])} risk entries, "
        f"~{_json_chars(material)} chars"
    )
    return material


def assemble_breaking_material(ev: Event, items: list[Item]) -> dict:
    """突发快讯素材: 单事件 + 其关联条目"""
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": _event_to_material(ev),
        "items": [_item_to_material(it) for it in items[:5]],
    }
