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

def _get_github_env() -> tuple[str, str]:
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

    # 截断过长消息
    if len(text) > _TELEGRAM_MAX_LENGTH:
        text = text[:_TELEGRAM_MAX_LENGTH - 100] + "\n\n[...消息过长已截断]"

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


def format_telegram_alert(
    new_events: list[Event],
    updated_events: list[Event],
    all_active_events: list[Event],
    new_items: list[Item],
    situation: Optional[Situation],
) -> str:
    """格式化 Telegram 推送 —— 只展示本轮新增和更新的事件"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        hkt = ZoneInfo("Asia/Hong_Kong")
    except Exception:
        hkt = None
    now_str = datetime.now(hkt).strftime("%m-%d %H:%M HKT") if hkt else ""

    lines = [f"*AI 投研雷达 · {now_str}*\n"]

    # —— 态势概述 ——
    if situation and situation.text:
        lines.append(f"_{situation.text}_\n")

    # —— 趋势信号（优先展示） ——
    if situation and situation.trend_spotting:
        lines.append(f"*趋势:*\n{situation.trend_spotting}\n")

    # —— 本轮新事件 ——
    if new_events:
        lines.append(f"*本轮新增事件 ({len(new_events)}):*")
        for ev in sorted(new_events, key=lambda e: e.significance, reverse=True)[:5]:
            cred = "🟢" if ev.significance >= 8 else "🟡" if ev.significance >= 6 else "🔴"
            tickers_str = f" [{', '.join(ev.tickers)}]" if ev.tickers else ""
            lines.append(
                f"\n{cred} *{ev.title}*{tickers_str}"
                f"\n  {ev.summary[:120]}{'...' if len(ev.summary) > 120 else ''}"
                f"\n  重要: {ev.significance}/10 | 来源: {ev.source_count}"
            )

    # —— 本轮更新的已有事件 ——
    if updated_events:
        lines.append(f"\n*本轮更新事件 ({len(updated_events)}):*")
        for ev in sorted(updated_events, key=lambda e: e.significance, reverse=True)[:5]:
            tickers_str = f" [{', '.join(ev.tickers)}]" if ev.tickers else ""
            lines.append(
                f"\n• *{ev.title}*{tickers_str}"
                f"\n  {ev.summary[:100]}{'...' if len(ev.summary) > 100 else ''}"
            )

    # —— 本轮精选条目（新增条目中的高价值内容） ——
    if new_items:
        top_items = sorted(new_items, key=lambda it: it.relevance_score, reverse=True)[:8]
        if top_items:
            lines.append(f"\n*本轮精选 ({len(top_items)}):*")
            for item in top_items:
                cred_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(item.credibility, "⚪")
                tickers_str = f" [{', '.join(item.tickers)}]" if item.tickers else ""
                lines.append(
                    f"\n{cred_emoji} {item.title}"
                    f"{tickers_str} · {item.relevance_score}分"
                    f"\n  {item.cn_summary[:100]}{'...' if len(item.cn_summary) > 100 else ''}"
                )

    # —— 统计栏 ——
    active_count = len([e for e in all_active_events if e.is_active])
    developing = len([e for e in all_active_events if e.is_active and e.status == "developing"])
    top_tickers: dict[str, int] = {}
    for ev in all_active_events:
        for tk in ev.tickers or []:
            top_tickers[tk] = top_tickers.get(tk, 0) + 1
    hot = sorted(top_tickers.items(), key=lambda x: x[1], reverse=True)[:5]
    hot_str = ", ".join(f"{tk}({n})" for tk, n in hot) if hot else "—"

    lines.append(
        f"\n———\n活跃事件: {active_count} | 演进中: {developing} | 最热标的: {hot_str}"
    )

    return "\n".join(lines)


# ================================================================
# README 更新
# ================================================================

def update_readme(issue_url: Optional[str] = None, site_url: str = "") -> None:
    """更新 README.md 顶部，放最新一期晨报链接"""
    readme_path = _ROOT / "README.md"
    today = today_str()

    header = f"""# AI 投研雷达

> AI/科技/半导体板块 · 滚动情报库 · 由 MiniMax 驱动策展
> 仅作为研究输入素材，不构成投资建议

## 最新日报

- [{today} 晨报]({issue_url or f'{site_url}/brief-{today}.md'})
- [实时看板]({site_url})
- [RSS 订阅]({site_url}/feed.xml)

---

"""
    if readme_path.exists():
        existing = readme_path.read_text(encoding="utf-8")
        # 保留 --- 之后的内容
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
    - 已有事件 direction 翻转
    - 距上次兜底推送 ≥ digest_interval_hours
    """
    telegram_cfg = cfg.get("channels", {}).get("telegram", {})

    # 新事件重要性高
    threshold = telegram_cfg.get("notify_new_event_threshold", 7)
    for ev in new_events:
        if ev.significance >= threshold:
            return True

    # 方向翻转
    if telegram_cfg.get("notify_direction_flip", True):
        for ev in updated_events:
            # 这里简化：有更新的活跃事件 default 推送
            if ev.significance >= 6:
                return True

    # 兜底推送间隔
    if situation and situation.last_telegram_digest_at:
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(
                situation.last_telegram_digest_at.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            interval = telegram_cfg.get("digest_interval_hours", 6)
            if (now - last).total_seconds() >= interval * 3600:
                return True
        except Exception:
            return True

    return False
