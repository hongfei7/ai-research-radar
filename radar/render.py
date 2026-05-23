"""渲染 —— 生成 RSS feed + GitHub Pages HTML + 日报 Markdown"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from radar.models import Item, Event, Situation, today_str
from radar.config import get_coverage_by_ticker
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


def _ensure_pages_dir() -> None:
    _PAGES_DIR.mkdir(parents=True, exist_ok=True)
    (_PAGES_DIR / "tickers").mkdir(exist_ok=True)
    (_PAGES_DIR / "themes").mkdir(exist_ok=True)


def _rfc2822(iso_str: str) -> str:
    """ISO8601 → RFC 2822 格式（用于 RSS pubDate）"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
    except Exception:
        return iso_str


def _now_hkt() -> str:
    try:
        from zoneinfo import ZoneInfo
        hkt = ZoneInfo("Asia/Hong_Kong")
    except Exception:
        hkt = timezone.utc
    return datetime.now(hkt).strftime("%Y-%m-%d %H:%M HKT")


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


def write_rss(items: list[Item], site_url: str = "", max_items: int = 50) -> Path:
    """生成并写入 pages/feed.xml"""
    _ensure_pages_dir()
    xml = render_rss(items, site_url, max_items)
    path = _PAGES_DIR / "feed.xml"
    path.write_text(xml, encoding="utf-8")
    logger.info(f"RSS feed written to {path} ({len(items[:max_items])} items)")
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
) -> Path:
    """生成并写入 pages/index.html"""
    _ensure_pages_dir()
    html = render_dashboard(items, events, situation, site_url)
    path = _PAGES_DIR / "index.html"
    path.write_text(html, encoding="utf-8")
    logger.info(f"Dashboard written to {path}")
    return path


# ================================================================
# 按标的/主线页面
# ================================================================

def write_ticker_pages(
    items: list[Item],
    events: list[Event],
    site_url: str = "",
) -> None:
    """为每个标的生成独立页面"""
    _ensure_pages_dir()
    template = _env.get_template("ticker.html.j2")

    # 按 ticker name 分组
    ticker_items: dict[str, list[Item]] = {}
    ticker_events: dict[str, list[Event]] = {}
    for it in items:
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


def write_theme_pages(
    items: list[Item],
    events: list[Event],
    site_url: str = "",
) -> None:
    """为每条投资主线生成独立页面（简化版）"""
    _ensure_pages_dir()
    # 类似 ticker 页面，这里简化处理
    pass


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

    themes_with_items = []
    for th_key, th_items in themes_map.items():
        # 找主线名称
        themes_with_items.append({
            "name": th_key,
            "items": th_items[:10],
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
            "items": tk_items[:15],
        })

    # 为每个 item 准备 direction_str
    for it in items:
        if it.direction:
            it.direction_str = ", ".join(
                f"{tk}→{d}" for tk, d in it.direction.items()
            )
        else:
            it.direction_str = ""

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
) -> Path:
    """生成并写入当日日报 Markdown"""
    _PAGES_DIR.mkdir(parents=True, exist_ok=True)
    md = render_daily_brief(items, synthesis, site_url)
    path = _PAGES_DIR / f"brief-{today_str()}.md"
    path.write_text(md, encoding="utf-8")
    logger.info(f"Daily brief written to {path}")
    return path
