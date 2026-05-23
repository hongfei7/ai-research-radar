"""渲染 —— 生成 RSS feed + GitHub Pages HTML + 日报 Markdown"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from radar.models import Item, Event, Situation, today_str, parse_iso
from radar.config import get_coverage_by_ticker, load_config, get_theme_by_key
from radar.credibility import CREDIBILITY_EMOJI, CREDIBILITY_LABEL

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_PAGES_DIR = Path(__file__).resolve().parent.parent / "pages"

# Jinja2 环境
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=False,  # RSS XML 不需要 HTML 转义
)
_env.globals["cred_emoji"] = CREDIBILITY_EMOJI.get
_env.globals["cred_label"] = CREDIBILITY_LABEL.get


def _format_direction(direction: dict) -> str:
    if not direction or not isinstance(direction, dict):
        return ""
    return ", ".join(f"{tk}→{d}" for tk, d in direction.items())


_env.filters["direction_str"] = _format_direction


def _rfc2822(iso_str: str) -> str:
    """ISO8601 → RFC 2822 格式（用于 RSS pubDate）"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
    except Exception:
        return iso_str


_env.filters["rfc2822"] = _rfc2822


def _ensure_pages_dir() -> None:
    _PAGES_DIR.mkdir(parents=True, exist_ok=True)
    (_PAGES_DIR / "tickers").mkdir(exist_ok=True)
    (_PAGES_DIR / "themes").mkdir(exist_ok=True)


def _now_hkt() -> str:
    try:
        from zoneinfo import ZoneInfo
        hkt = ZoneInfo("Asia/Hong_Kong")
    except Exception:
        hkt = timezone.utc
    return datetime.now(hkt).strftime("%Y-%m-%d %H:%M HKT")


def _filter_recent(items: list[Item], window_hours: int = 8) -> list[Item]:
    """只保留最近 window_hours 内发布/处理的条目"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    result = []
    for it in items:
        # 优先用 published_at，其次 processed_at，再次 fetched_at
        dt = parse_iso(it.published_at) or parse_iso(it.processed_at) or parse_iso(it.fetched_at)
        if dt is None or dt >= cutoff:
            result.append(it)
    return result


# ================================================================
# RSS Feed
# ================================================================

def render_rss(
    items: list[Item],
    site_url: str = "https://USER.github.io/ai-research-radar",
    max_items: int = 50,
) -> str:
    """生成 RSS feed XML 字符串"""
    template = _env.get_template("feed.xml.j2")
    return template.render(
        items=items[:max_items],
        site_url=site_url,
        build_date=_rfc2822(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )


def write_rss(
    items: list[Item], site_url: str = "", max_items: int = 50, window_hours: int = 8
) -> Path:
    """生成并写入 pages/feed.xml，仅包含时间窗口内的条目"""
    _ensure_pages_dir()
    recent = _filter_recent(items, window_hours)
    xml = render_rss(recent, site_url, max_items)
    path = _PAGES_DIR / "feed.xml"
    path.write_text(xml, encoding="utf-8")
    logger.info(f"RSS feed written to {path} ({len(recent[:max_items])} recent items)")
    return path


# ================================================================
# GitHub Pages 看板
# ================================================================

def render_dashboard(
    items: list[Item],
    events: list[Event],
    situation: Optional[Situation],
    site_url: str = "",
) -> str:
    """生成首页看板 HTML"""
    template = _env.get_template("dashboard.html.j2")

    # 收集所有 ticker
    all_tickers = sorted(set(
        tk for ev in events for tk in (ev.tickers or [])
    ).union(
        tk for it in items for tk in (it.tickers or [])
    ))

    return template.render(
        items=items[:60],
        events=events[:30],
        situation=situation,
        site_url=site_url,
        updated_at=_now_hkt(),
        all_tickers=all_tickers,
    )


def write_dashboard(
    items: list[Item],
    events: list[Event],
    situation: Optional[Situation],
    site_url: str = "",
    window_hours: int = 8,
) -> Path:
    """生成并写入 pages/index.html，仅展示时间窗口内的内容"""
    _ensure_pages_dir()
    recent = _filter_recent(items, window_hours)
    html = render_dashboard(recent, events, situation, site_url)
    path = _PAGES_DIR / "index.html"
    path.write_text(html, encoding="utf-8")
    logger.info(f"Dashboard written to {path} ({len(recent)} recent items)")
    return path


# ================================================================
# 按标的/主线页面
# ================================================================

def write_ticker_pages(
    items: list[Item],
    events: list[Event],
    site_url: str = "",
    window_hours: int = 8,
) -> None:
    """为每个标的生成独立页面（仅展示时间窗口内的条目）"""
    _ensure_pages_dir()
    template = _env.get_template("ticker.html.j2")
    recent = _filter_recent(items, window_hours)

    # 按 ticker name 分组
    ticker_items: dict[str, list[Item]] = {}
    ticker_events: dict[str, list[Event]] = {}
    for it in recent:
        for tk in it.tickers or []:
            ticker_items.setdefault(tk, []).append(it)
    for ev in events:
        for tk in ev.tickers or []:
            ticker_events.setdefault(tk, []).append(ev)

    for tk_name in set(list(ticker_items) + list(ticker_events)):
        html = template.render(
            ticker_name=tk_name,
            ticker="",  # 可从 config 查找，这里简化
            items=ticker_items.get(tk_name, [])[:30],
            events=ticker_events.get(tk_name, [])[:20],
            site_url=site_url,
        )
        path = _PAGES_DIR / "tickers" / f"{tk_name}.html"
        path.write_text(html, encoding="utf-8")
        logger.info(f"Ticker page written: {path}")


# ================================================================
# 日报 Markdown
# ================================================================

def render_daily_brief(
    items: list[Item],
    synthesis: str,
    site_url: str = "",
) -> str:
    """生成日报 Markdown"""
    today = today_str()
    template = _env.get_template("brief.md.j2")

    # 按主题分组
    themes_map: dict[str, list[Item]] = {}
    for it in items:
        for th in it.themes or []:
            themes_map.setdefault(th, []).append(it)

    cfg = load_config()
    themes_with_items = []
    for th_key, th_items in themes_map.items():
        theme_info = get_theme_by_key(cfg, th_key)
        display_name = theme_info["name"] if theme_info else th_key
        themes_with_items.append({
            "name": display_name,
            "entries": th_items[:10],
        })

    # 按标的分组
    tickers_map: dict[str, list[Item]] = {}
    for it in items:
        for tk in it.tickers or []:
            tickers_map.setdefault(tk, []).append(it)

    tickers_with_items = []
    for tk_name, tk_items in tickers_map.items():
        tickers_with_items.append({
            "name": tk_name,
            "ticker": "",
            "entries": tk_items[:15],
        })

    return template.render(
        date=today,
        synthesis=synthesis,
        themes_with_items=themes_with_items,
        tickers_with_items=tickers_with_items,
        site_url=site_url,
        generated_at=_now_hkt(),
    )


def write_daily_brief(
    items: list[Item],
    synthesis: str,
    site_url: str = "",
    window_hours: int = 24,
) -> Path:
    """生成并写入当日日报 Markdown（日报窗口更长，覆盖全天）"""
    _PAGES_DIR.mkdir(parents=True, exist_ok=True)
    recent = _filter_recent(items, window_hours)
    md = render_daily_brief(recent, synthesis, site_url)
    path = _PAGES_DIR / f"brief-{today_str()}.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"Daily brief written to {path} ({len(recent)} recent items)")
    return path
