"""数据装配层 —— 按时间窗从 events.json + archive/*.jsonl 加载撰稿素材

本层独立按时间窗装配, 不依赖"本轮 clustered_items"(审计 S2)。

两条硬约束:
1. 每个事件带一个短 ref(E1…En)与 sources 链接清单。撰稿 LLM 只能引用 ref,
   URL 由渲染层从 sources 解析 —— 让 LLM 直接产出链接必然出现幻觉链接,
   而一份不可溯源的研报没有使用价值。
2. token 预算: 输入素材控制在 ~22000 字符, 按 significance 降序裁剪。

宏观素材(situation / cross_analysis / trend_spotting)与跨期素材(last_report)
在正文里有专门章节承载, 因此给了比过去更宽的裁剪额度。
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from radar.models import Item, Event, Situation, parse_iso, get_event_effective_date
from radar.storage import load_events, load_items, load_situation
from radar.textnorm import clean_title, clip_sentence, strip_markdown

logger = logging.getLogger(__name__)

# 素材总字符预算(粗略 2 chars ≈ 1 token)
_MAX_MATERIAL_CHARS = 22000
# 单个字段裁剪
_EVENT_SUMMARY_CLIP = 220
_EVENT_ANALYSIS_CLIP = 300
_COUNTERPOINT_CLIP = 220
_ITEM_SUMMARY_CLIP = 150
# 每个事件最多带几条来源链接
_MAX_SOURCES_PER_EVENT = 4
# 反面观点的最低相关性要求(低于此分的反向观点多是模板化空话)
_COUNTERPOINT_MIN_SCORE = 7

_CRED_RANK = {"high": 0, "medium": 1, "low": 2}

# second_opinion 常见的套话开头, 渲染前剥掉
_COUNTERPOINT_LEADS = (
    "反向观点补充", "反向观点", "补充观点", "替代解读", "以下是反向观点",
)


def _clip(text: str, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


def _window_cutoff(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _cred_rank(cred: str) -> int:
    c = (cred or "").strip().lower()
    for level, rank in _CRED_RANK.items():
        if level in c:
            return rank
    return 2


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
    items: dict[str, Item] = {}
    # archive 按 HKT 日期分文件, 窗口横跨 N 天逐个加载
    from radar.models import today_str
    days = min(8, max(1, int(hours // 24) + 1))
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


def _source_entry(it: Item) -> dict:
    return {
        "title": clean_title(it.title),
        "url": it.url,
        "source": it.source,
        "published_at": it.published_at,
        "credibility": it.credibility,
        "is_primary_source": bool(it.is_primary_source),
    }


def _collect_sources(ev: Event, items_index: dict[str, Item]) -> list[dict]:
    """取事件的代表来源: 一手优先 → 可信度优先 → 时间早者优先(首报)

    跨日归档里找不到的 item_id 直接跳过 —— 宁可少给链接, 也不给死链。
    """
    members = [items_index[i] for i in ev.item_ids if i in items_index]
    members.sort(key=lambda it: (
        not bool(it.is_primary_source),
        _cred_rank(it.credibility),
        it.published_at or "9999",
    ))
    seen_urls: set[str] = set()
    out: list[dict] = []
    for it in members:
        if not it.url or it.url in seen_urls:
            continue
        seen_urls.add(it.url)
        out.append(_source_entry(it))
        if len(out) >= _MAX_SOURCES_PER_EVENT:
            break
    return out


def _pick_counterpoint(ev: Event, items_index: dict[str, Item]) -> str:
    """事件的反面观点: 取成员条目里分数最高的一条 second_opinion

    过去按"低可信"筛风险素材, 但低可信条目占入库量的 57%, 结果附录变成
    低质条目的随机倾倒(审计发现)。改为只认显式生成的反向观点。
    """
    best = ""
    best_score = -1
    for iid in ev.item_ids:
        it = items_index.get(iid)
        if it is None or not (it.second_opinion or "").strip():
            continue
        if (it.relevance_score or 0) < _COUNTERPOINT_MIN_SCORE:
            continue
        if (it.relevance_score or 0) > best_score:
            best_score = it.relevance_score or 0
            best = it.second_opinion
    if not best:
        return ""
    text = strip_markdown(best)
    for lead in _COUNTERPOINT_LEADS:
        if text.startswith(lead):
            text = text[len(lead):].lstrip("：: 　")
            break
    return clip_sentence(text, _COUNTERPOINT_CLIP)


def _event_to_material(ev: Event, ref: str, items_index: dict[str, Item]) -> dict:
    return {
        "ref": ref,
        "event_id": ev.event_id,
        "title": clean_title(ev.title),
        "summary": _clip(ev.summary, _EVENT_SUMMARY_CLIP),
        "tickers": (ev.tickers or [])[:4],
        "themes": ev.themes or [],
        "direction": ev.direction or {},
        "significance": ev.significance,
        "status": ev.status,
        "is_active": ev.is_active,
        "source_count": ev.source_count,
        "first_seen_at": ev.first_seen_at,
        "last_updated_at": ev.last_updated_at,
        "deep_analysis": _clip(ev.deep_analysis, _EVENT_ANALYSIS_CLIP),
        "counterpoint": _pick_counterpoint(ev, items_index),
        "sources": _collect_sources(ev, items_index),
    }


def _item_to_material(it: Item) -> dict:
    return {
        "title": clean_title(it.title),
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


def _last_report_material(last_report: Optional[dict]) -> Optional[dict]:
    """上一期的宏观框架与判断, 供本期做框架增量修订与证伪回溯

    只带 claim / falsifier / verify, 不带正文 —— 回溯需要的是"上期承诺检验什么",
    不是重读上期全文。
    """
    if not last_report:
        return None
    calls = []
    for c in (last_report.get("calls") or [])[:5]:
        calls.append({
            "claim": _clip(c.get("claim", ""), 120),
            "falsifier": _clip(c.get("falsifier", ""), 160),
            "verify": _clip(c.get("verify", ""), 80),
        })
    if not calls and not last_report.get("macro"):
        return None
    return {
        "date": last_report.get("date", ""),
        "macro": last_report.get("macro") or {},
        "calls": calls,
    }


def assemble_material(
    hours: float,
    situation: Optional[Situation] = None,
    themes_map: Optional[dict] = None,
    last_report: Optional[dict] = None,
) -> dict:
    """装配撰稿素材

    Returns:
        {
          "window_hours", "generated_at", "stats",
          "situation", "trend_spotting", "cross_analysis", "key_themes",
          "events": [ {ref, title, …, counterpoint, sources[]} ],
          "last_report": {...} | None,
          "dropped_events": N,
        }
    """
    if situation is None:
        situation = load_situation()
    events = load_window_events(hours)

    try:
        items = load_window_items(hours)
    except Exception as e:
        logger.warning(f"assemble: load items failed: {e}")
        items = []
    items_index = {it.id: it for it in items}

    material: dict = {
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": {"items_ingested": len(items), "events": len(events)},
        "situation": _clip(situation.text, 600) if situation else "",
        "trend_spotting": _clip(situation.trend_spotting, 700) if situation else "",
        "cross_analysis": _clip(situation.cross_analysis, 900) if situation else "",
        "key_themes": situation.key_themes if situation else [],
        "themes_map": themes_map or {},
        "last_report": _last_report_material(last_report),
        "events": [],
        "dropped_events": 0,
    }

    base_chars = _json_chars({k: v for k, v in material.items() if k != "events"})
    budget = _MAX_MATERIAL_CHARS - base_chars

    # 主素材: 按 significance 降序填入, 超预算即截断。
    # ref 按实际入选顺序编号, 保证 LLM 看到的 ref 集合连续无空洞。
    used = 0
    for ev in events:
        ref = f"E{len(material['events']) + 1}"
        entry = _event_to_material(ev, ref, items_index)
        # 按 LLM 实际会看到的体积计价(URL 会被 llm_material 剥掉, 不占预算)
        cost = _json_chars(entry) - sum(
            len(s.get("url") or "") + 10 for s in entry["sources"]
        )
        if used + cost > budget:
            material["dropped_events"] += 1
            continue
        material["events"].append(entry)
        used += cost

    with_sources = sum(1 for e in material["events"] if e["sources"])
    logger.info(
        f"Assembled material: {len(material['events'])} events "
        f"({with_sources} with sources, {material['dropped_events']} dropped), "
        f"{len(items)} items in window, ~{_json_chars(material)} chars"
    )
    return material


def material_refs(material: dict) -> set[str]:
    """素材里所有合法的证据 ref, 用于校验 LLM 引用"""
    return {e.get("ref", "") for e in (material.get("events") or []) if e.get("ref")}


def llm_material(material: dict) -> dict:
    """喂给撰稿 LLM 的素材副本: 剥掉所有 URL

    LLM 看不到链接就不可能编出链接 —— 引用一律走 ref, 渲染层再还原。
    顺带省下每条来源约 80 字符的预算, 让更多事件挤进同一个 token 窗口。
    """
    stripped = dict(material)
    stripped["events"] = [
        {
            **{k: v for k, v in ev.items() if k != "sources"},
            "sources": [
                {k: v for k, v in s.items() if k != "url"}
                for s in (ev.get("sources") or [])
            ],
        }
        for ev in (material.get("events") or [])
    ]
    return stripped


def sources_by_ref(material: dict) -> dict[str, list[dict]]:
    """ref → 来源链接清单, 渲染层据此把 LLM 的引用还原成可点击链接"""
    return {
        e["ref"]: e.get("sources") or []
        for e in (material.get("events") or []) if e.get("ref")
    }


def assemble_breaking_material(ev: Event, items: list[Item]) -> dict:
    """突发快讯素材: 单事件 + 其关联条目"""
    items_index = {it.id: it for it in items}
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": _event_to_material(ev, "E1", items_index),
        "items": [_item_to_material(it) for it in items[:5]],
    }
