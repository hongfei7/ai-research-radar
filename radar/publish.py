"""分发 —— GitHub Issue + Telegram + README 更新"""

import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from radar.models import Item, Event, Situation, today_str

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# 最大 Telegram 消息长度
_TELEGRAM_MAX_LENGTH = 4096


# ================================================================
# GitHub Issue
# ================================================================

def _get_github_env() -> tuple[str, str, str]:
    """从 GitHub Actions 环境获取 repo 信息"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    return repo, token, api_url


async def create_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> Optional[str]:
    """
    创建 GitHub Issue。

    Returns:
        Issue HTML URL，失败返回 None
    """
    repo, token, api_url = _get_github_env()
    if not repo or not token:
        logger.warning("GITHUB_REPOSITORY or GITHUB_TOKEN not set, skipping issue creation")
        return None

    url = f"{api_url}/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "title": title,
        "body": body,
        "labels": labels or [],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            issue_url = data.get("html_url", "")
            logger.info(f"Issue created: {issue_url}")
            return issue_url
    except Exception as e:
        logger.error(f"Failed to create issue: {e}")
        return None


async def create_daily_issue(
    brief_md: str,
    label: str = "晨报",
) -> Optional[str]:
    """为日报创建 Issue"""
    today = today_str()
    title = f"AI 投研雷达 · 日报 · {today}"
    return await create_issue(title, brief_md, [label])


# ================================================================
# Telegram
# ================================================================

def _get_telegram_env() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat_id


async def send_telegram(
    text: str,
    parse_mode: str = "Markdown",
) -> bool:
    """
    发送 Telegram 消息。

    Returns:
        True 如果发送成功
    """
    token, chat_id = _get_telegram_env()
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping")
        return False

    # 兜底截断：优先在段落/句子边界处断，保护可读性
    if len(text) > _TELEGRAM_MAX_LENGTH:
        budget = _TELEGRAM_MAX_LENGTH - 50
        last_break = text.rfind("\n\n", 0, budget)
        if last_break > budget * 0.5:
            text = text[:last_break] + "\n\n[...完整版见实时看板]"
        else:
            cut = text[:budget].rfind("。")
            if cut > budget * 0.5:
                text = text[:cut + 1] + "\n\n[...完整版见实时看板]"
            else:
                text = text[:budget] + "\u2026\n\n[...完整版见实时看板]"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                logger.info("Telegram message sent")
                return True
            else:
                logger.error(f"Telegram API error: {data}")
                return False
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


# —— 格式化辅助函数 ——

def _clip(text: str, max_len: int) -> str:
    """截断文本，优先在句号处断句"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    for sep in ("。", "\n", "；", "，"):
        cut = text[:max_len].rfind(sep)
        if cut > max_len * 0.5:
            return text[:cut + 1]
    return text[:max_len - 1] + "\u2026"


def _fmt_tickers(tickers: list[str], max_display: int = 4) -> str:
    """紧凑标的展示，超过上限显示 +N"""
    if not tickers:
        return ""
    display = tickers[:max_display]
    s = ", ".join(display)
    if len(tickers) > max_display:
        s += f" +{len(tickers) - max_display}"
    return f" [{s}]"


def _extract_headline(text: str, max_len: int = 200) -> str:
    """从长文本中提取 1-2 句作为标题式概述"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    first_period = text.find("。")
    if first_period == -1 or first_period > max_len:
        return _clip(text, max_len)
    second_period = text.find("。", first_period + 1)
    if second_period != -1 and second_period < max_len:
        return text[:second_period + 1]
    return text[:first_period + 1]


# —— 主格式化函数 ——

def format_telegram_alert(
    new_events: list[Event],
    updated_events: list[Event],
    all_active_events: list[Event],
    new_items: list[Item],
    situation: Optional[Situation],
    site_url: str = "",
) -> str:
    """格式化 Telegram 推送 —— 言简意赅，保留必要的判断依据

    设计原则:
    - 高频推送，每 30min 一次，用户会自行延伸研究重要内容
    - 每个事件必须保留: 标题 + 关键标的 + 摘要 + 重要性评分 + 来源数
    - 不硬截断: 优先牺牲低优先级内容（趋势 → 态势 → 更新事件 → 新增事件）
    - 事件去重: 同时出现在 new/updated 中仅展示一次
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        hkt = ZoneInfo("Asia/Hong_Kong")
    except Exception:
        hkt = None
    now_str = datetime.now(hkt).strftime("%m-%d %H:%M HKT") if hkt else ""

    MAX_TOTAL = 3900

    # (label, text, priority): 0=required, 1=high, 2=medium, 3=low
    parts: list[tuple[str, str, int]] = []

    # —— 1. 标题行 (priority 0) ——
    parts.append(("header", f"*AI 投研雷达 \u00b7 {now_str}*\n", 0))

    # —— 2. 态势概要 (priority 2) ——
    if situation and situation.text:
        headline = _extract_headline(situation.text, 200)
        if headline:
            parts.append(("situation", f"_{headline}_\n\n", 2))

    # —— 3. 本轮新事件 (priority 0, top 6 → trim to 3) ——
    updated_ids = {ev.event_id for ev in updated_events}
    deduped_new = [ev for ev in new_events if ev.event_id not in updated_ids]
    if deduped_new:
        sorted_new = sorted(deduped_new, key=lambda e: e.significance, reverse=True)
        new_section = f"*\U0001f525 新增 ({len(deduped_new)}):*\n"
        for ev in sorted_new[:6]:
            flag = "\U0001f7e2" if ev.significance >= 8 else "\U0001f7e1" if ev.significance >= 6 else "\U0001f534"
            tickers_str = _fmt_tickers(ev.tickers, max_display=5)
            summary = _clip(ev.summary or "", 90)
            new_section += (
                f"{flag} *{_clip(ev.title or '', 42)}*{tickers_str}\n"
                f"  {summary} | {ev.significance}/10 | {ev.source_count}\u6e90\n"
            )
        parts.append(("new_events", new_section + "\n", 0))

    # —— 4. 重要更新 (priority 1, top 4 → trim to 2) ——
    new_ids = {ev.event_id for ev in new_events}
    deduped_upd = [ev for ev in updated_events if ev.event_id not in new_ids]
    if deduped_upd:
        sorted_upd = sorted(deduped_upd, key=lambda e: e.significance, reverse=True)
        upd_section = f"*\U0001f4cc 更新 ({len(deduped_upd)}):*\n"
        for ev in sorted_upd[:4]:
            tickers_str = _fmt_tickers(ev.tickers, max_display=4)
            summary = _clip(ev.summary or "", 70)
            upd_section += (
                f"\u2022 *{_clip(ev.title or '', 42)}*{tickers_str}\n"
                f"  {summary} | {ev.significance}/10\n"
            )
        parts.append(("updated_events", upd_section + "\n", 1))

    # —— 5. 趋势信号 (priority 3 — trimmed first) ——
    if situation and situation.trend_spotting and (deduped_new or deduped_upd):
        trend = _clip(situation.trend_spotting.strip(), 350)
        if trend:
            parts.append(("trend", f"*\U0001f4e1 趋势:*\n{trend}\n\n", 3))

    # —— 6. 统计栏 + 链接 (priority 0) ——
    active_count = len([e for e in all_active_events if e.is_active])
    developing = len([e for e in all_active_events if e.is_active and e.status == "developing"])
    top_tickers: dict[str, int] = {}
    for ev in all_active_events:
        for tk in ev.tickers or []:
            top_tickers[tk] = top_tickers.get(tk, 0) + 1
    hot = sorted(top_tickers.items(), key=lambda x: x[1], reverse=True)[:5]
    hot_str = ", ".join(f"{tk}({n})" for tk, n in hot) if hot else "\u2014"

    footer = f"\u2014\u2014\u2014\n\u6d3b\u8dc3 {active_count} | \u6f14\u8fdb {developing} | \u70ed\u6807 {hot_str}"
    if site_url:
        footer += f"\n[\u5b9e\u65f6\u770b\u677f]({site_url})"
    parts.append(("footer", footer, 0))

    # —— 组装 + 软性空间控制 ——
    def _assemble(pts: list[tuple[str, str, int]]) -> str:
        return "".join(p[1] for p in pts)

    full_text = _assemble(parts)
    if len(full_text) <= MAX_TOTAL:
        return full_text.rstrip()

    # Step 1: 压缩趋势 (priority 3) — 350 → 200 chars
    for i, (label, text, pri) in enumerate(parts):
        if pri == 3 and situation and situation.trend_spotting:
            short_trend = _clip(situation.trend_spotting.strip(), 200)
            parts[i] = (label, f"*\U0001f4e1 趋势:*\n{short_trend}\n\n", 3)
            if len(_assemble(parts)) <= MAX_TOTAL:
                return _assemble(parts).rstrip()

    # Step 2: 压缩态势 (priority 2) — 200 → 120 chars
    for i, (label, text, pri) in enumerate(parts):
        if pri == 2 and situation and situation.text:
            shorter = _extract_headline(situation.text, 120)
            parts[i] = (label, f"_{shorter}_\n\n" if shorter else "", 2)
            if len(_assemble(parts)) <= MAX_TOTAL:
                return _assemble(parts).rstrip()

    # Step 3: 减少更新事件 (priority 1) — 4 → 2
    for i, (label, text, pri) in enumerate(parts):
        if pri == 1 and deduped_upd:
            sorted_upd = sorted(deduped_upd, key=lambda e: e.significance, reverse=True)
            reduced = f"*\U0001f4cc 更新 ({len(deduped_upd)}):*\n"
            for ev in sorted_upd[:2]:
                tickers_str = _fmt_tickers(ev.tickers, max_display=3)
                summary = _clip(ev.summary or "", 60)
                reduced += (
                    f"\u2022 *{_clip(ev.title or '', 40)}*{tickers_str}\n"
                    f"  {summary}\n"
                )
            parts[i] = (label, reduced + "\n", 1)
            if len(_assemble(parts)) <= MAX_TOTAL:
                return _assemble(parts).rstrip()

    # Step 4: 减少新事件 (priority 0) — 6 → 3
    for i, (label, text, pri) in enumerate(parts):
        if pri == 0 and label == "new_events" and deduped_new:
            sorted_new = sorted(deduped_new, key=lambda e: e.significance, reverse=True)
            reduced = f"*\U0001f525 新增 ({len(deduped_new)}):*\n"
            for ev in sorted_new[:3]:
                flag = "\U0001f7e2" if ev.significance >= 8 else "\U0001f7e1" if ev.significance >= 6 else "\U0001f534"
                tickers_str = _fmt_tickers(ev.tickers, max_display=4)
                summary = _clip(ev.summary or "", 80)
                reduced += (
                    f"{flag} *{_clip(ev.title or '', 40)}*{tickers_str}\n"
                    f"  {summary} | {ev.significance}/10\n"
                )
            parts[i] = (label, reduced + "\n", 0)
            if len(_assemble(parts)) <= MAX_TOTAL:
                return _assemble(parts).rstrip()

    # Step 5: 最终兜底 — header + new events (3) + footer
    result_parts = []
    for label, text, pri in parts:
        if pri == 0 and label in ("header", "footer"):
            result_parts.append(text)
        elif pri == 0 and label == "new_events":
            result_parts.append(text)
    result = "".join(result_parts)
    if len(result) > MAX_TOTAL:
        result = result[:MAX_TOTAL - 30] + "\u2026\n[...\u5b8c\u6574\u7248\u89c1\u5b9e\u65f6\u770b\u677f]"
    return result.rstrip()


# ================================================================
# README 更新
# ================================================================

def update_readme(issue_url: Optional[str] = None, site_url: str = "") -> None:
    """更新 README.md 顶部，放最新一期晨报链接"""
    readme_path = _ROOT / "README.md"
    today = today_str()

    header = f"""# AI 投研雷达

> AI/科技/半导体板块 \u00b7 滚动情报库 \u00b7 由 MiniMax 驱动策展
> 仅作为研究输入素材，不构成投资建议

## 最新日报

- [{today} 晨报]({issue_url or f'{site_url}/brief-{today}.md'})
- [实时看板]({site_url})
- [RSS 订阅]({site_url}/feed.xml)

---

"""
    if readme_path.exists():
        existing = readme_path.read_text(encoding="utf-8")
        parts = existing.split("---", 1)
        if len(parts) > 1:
            header += "---" + parts[1]

    readme_path.write_text(header, encoding="utf-8")
    logger.info(f"README updated: {readme_path}")


# ================================================================
# Telegram 智能推送判断
# ================================================================

def should_telegram_alert(
    new_events: list[Event],
    updated_events: list[Event],
    situation: Optional[Situation],
    cfg: dict,
) -> bool:
    """
    判断是否需要推送 Telegram：
    - 新事件 significance >= threshold
    - 已有事件重要更新
    - 距上次兜底推送 >= digest_interval_hours
    """
    telegram_cfg = cfg.get("channels", {}).get("telegram", {})

    threshold = telegram_cfg.get("notify_new_event_threshold", 7)
    for ev in new_events:
        if ev.significance >= threshold:
            return True

    notify_update = telegram_cfg.get("notify_direction_flip", True)
    if notify_update:
        for ev in updated_events:
            if ev.significance >= threshold:
                return True

    if situation and situation.last_telegram_digest_at:
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(
                situation.last_telegram_digest_at.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            interval = telegram_cfg.get("digest_interval_hours", 1)
            if (now - last).total_seconds() >= interval * 3600:
                return True
        except Exception:
            return True

    return False
