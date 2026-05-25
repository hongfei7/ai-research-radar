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
# 微信推送（PushPlus）
# ================================================================

def _get_wechat_env() -> str:
    return os.environ.get("WECHAT_PUSH_TOKEN", "")


async def send_wechat(
    title: str,
    content: str,
) -> bool:
    """
    通过 PushPlus 推送到微信。

    Args:
        title: 消息标题（必填）
        content: 消息正文，支持 Markdown

    Returns:
        True 如果发送成功
    """
    import asyncio

    token = _get_wechat_env()
    if not token:
        logger.warning("WECHAT_PUSH_TOKEN not set, skipping WeChat push")
        return False

    url = "https://www.pushplus.plus/send"
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "markdown",
    }

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 200:
                    logger.info("WeChat message sent via PushPlus")
                    return True
                else:
                    logger.error(f"PushPlus API error: {data}")
                    if attempt < max_retries:
                        logger.info(f"Retrying WeChat push ({attempt + 1}/{max_retries})...")
                        await asyncio.sleep(2)
                        continue
                    return False
        except Exception as e:
            logger.error(f"Failed to send WeChat message (attempt {attempt + 1}): {e}")
            if attempt < max_retries:
                logger.info(f"Retrying WeChat push ({attempt + 1}/{max_retries})...")
                await asyncio.sleep(2)
            else:
                return False

    return False


def _fmt_tickers_wechat(tickers: list[str], max_display: int = 5) -> str:
    """微信格式标的展示：紧凑方括号，超出显示 +N"""
    if not tickers:
        return ""
    display = tickers[:max_display]
    s = ", ".join(display)
    if len(tickers) > max_display:
        s += f" +{len(tickers) - max_display}"
    return f"[{s}]"


def format_wechat_alert(
    new_events: list[Event],
    updated_events: list[Event],
    all_active_events: list[Event],
    situation: Optional[Situation],
    site_url: str = "",
) -> tuple[str, str]:
    """格式化微信推送（WeChat-first），返回 (title, content)

    专为 PushPlus 微信 Markdown 渲染优化：
    - 使用 ### 三级标题替代粗体段落分隔
    - 摘要独占一行，评分/来源另起一行（更宽松的排版）
    - 趋势信号使用 > blockquote 展示
    - 无 Telegram 硬截断限制，合理精选 top 6/4 事件
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        hkt = ZoneInfo("Asia/Hong_Kong")
    except Exception:
        hkt = None
    now_str = datetime.now(hkt).strftime("%m-%d %H:%M HKT") if hkt else ""

    title = f"AI 投研雷达 · {now_str}"

    lines: list[str] = []

    # —— 态势概要 ——
    if situation and situation.text:
        headline = _extract_headline(situation.text, 200)
        if headline:
            lines.append(f"> {headline}")
            lines.append("")

    # —— 本轮新事件（top 6） ——
    updated_ids = {ev.event_id for ev in updated_events}
    deduped_new = [ev for ev in new_events if ev.event_id not in updated_ids]
    if deduped_new:
        sorted_new = sorted(deduped_new, key=lambda e: e.significance, reverse=True)
        lines.append(f"### 🔥 本轮新增（{len(deduped_new)}）")
        lines.append("")
        for ev in sorted_new[:6]:
            flag = "🟢" if ev.significance >= 8 else "🟡" if ev.significance >= 6 else "🔴"
            tickers_str = _fmt_tickers_wechat(ev.tickers, max_display=5)
            summary = _clip(ev.summary or "", 150)
            lines.append(f"{flag} **{_clip(ev.title or '', 60)}**")
            if tickers_str:
                lines.append(f"{tickers_str} · {ev.significance}/10 · {ev.source_count} 个来源")
            else:
                lines.append(f"{ev.significance}/10 · {ev.source_count} 个来源")
            if summary:
                lines.append(f"{summary}")
            lines.append("")
        lines.append("")

    # —— 重要更新（top 4） ——
    new_ids = {ev.event_id for ev in new_events}
    deduped_upd = [ev for ev in updated_events if ev.event_id not in new_ids]
    if deduped_upd:
        sorted_upd = sorted(deduped_upd, key=lambda e: e.significance, reverse=True)
        lines.append(f"### 📌 重要更新（{len(deduped_upd)}）")
        lines.append("")
        for ev in sorted_upd[:4]:
            tickers_str = _fmt_tickers_wechat(ev.tickers, max_display=5)
            summary = _clip(ev.summary or "", 150)
            lines.append(f"**{_clip(ev.title or '', 60)}**")
            if tickers_str:
                lines.append(f"{tickers_str} · {ev.significance}/10")
            else:
                lines.append(f"{ev.significance}/10")
            if summary:
                lines.append(f"{summary}")
            lines.append("")
        lines.append("")

    # —— 交叉综合分析 ——
    if situation and situation.cross_analysis and (deduped_new or deduped_upd):
        ca = _clip(situation.cross_analysis.strip(), 600)
        if ca:
            lines.append("### 🔬 交叉分析")
            lines.append("")
            lines.append(f"> {ca}")
            lines.append("")

    # —— 趋势信号 ——
    if situation and situation.trend_spotting and (deduped_new or deduped_upd):
        trend = _clip(situation.trend_spotting.strip(), 600)
        if trend:
            lines.append("### 📡 趋势信号")
            lines.append("")
            lines.append(f"> {trend}")
            lines.append("")

    # —— 统计栏 ——
    active_count = len([e for e in all_active_events if e.is_active])
    developing = len([e for e in all_active_events if e.is_active and e.status == "developing"])
    top_tickers: dict[str, int] = {}
    for ev in all_active_events:
        for tk in ev.tickers or []:
            top_tickers[tk] = top_tickers.get(tk, 0) + 1
    hot = sorted(top_tickers.items(), key=lambda x: x[1], reverse=True)[:5]
    hot_str = ", ".join(f"{tk}({n})" for tk, n in hot) if hot else "—"

    footer = f"---  \n活跃 {active_count} | 演进 {developing} | 热标 {hot_str}"
    if site_url:
        footer += f"  \n[实时看板]({site_url})"
    lines.append(footer)

    content = "\n".join(lines)
    return title, content


async def send_wechat_brief(
    title: str,
    brief_md: str,
    issue_url: str = "",
    site_url: str = "",
) -> bool:
    """推送晨报到微信

    将晨报 Markdown 包装成适合微信 PushPlus 的格式并发送。
    """
    content = f"# {title}\n\n{brief_md}"
    if issue_url:
        content += f"\n\n---\n[查看 Issue]({issue_url}) | [实时看板]({site_url})"
    elif site_url:
        content += f"\n\n---\n[实时看板]({site_url})"
    return await send_wechat(title, content)


def should_wechat_alert(
    new_events: list[Event],
    updated_events: list[Event],
    situation: Optional[Situation],
    cfg: dict,
) -> bool:
    """判断是否需要推送微信（与 Telegram 相同逻辑，独立状态）"""
    wechat_cfg = cfg.get("channels", {}).get("wechat", {})

    threshold = wechat_cfg.get("notify_new_event_threshold", 7)
    for ev in new_events:
        if ev.significance >= threshold and ev.source_count <= 3:
            return True

    notify_update = wechat_cfg.get("notify_direction_flip", True)
    if notify_update and updated_events:
        for ev in updated_events:
            if ev.significance >= threshold:
                return True

    # 兜底推送间隔
    if situation and situation.last_wechat_digest_at:
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(
                situation.last_wechat_digest_at.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            interval = wechat_cfg.get("digest_interval_hours", 2)
            if (now - last).total_seconds() >= interval * 3600:
                return True
        except Exception:
            return True

    return False


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
    - 有真正的新事件（本轮创建，非合并到旧事件）
    - 已有事件方向发生翻转或重要性显著提升
    - 距上次兜底推送 ≥ digest_interval_hours
    """
    telegram_cfg = cfg.get("channels", {}).get("telegram", {})

    # 真正的新事件：本轮创建、且重要性达标
    threshold = telegram_cfg.get("notify_new_event_threshold", 7)
    for ev in new_events:
        if ev.significance >= threshold and ev.source_count <= 3:
            # source_count 小 = 新事件，而非累积了很多来源的老事件
            return True

    # 已有事件重要更新：source_count 增长（说明有实质性的新信息加入）
    notify_update = telegram_cfg.get("notify_direction_flip", True)
    if notify_update and updated_events:
        for ev in updated_events:
            if ev.significance >= threshold:
                return True

    # 兜底推送间隔（确保不会完全沉默）
    if situation and situation.last_telegram_digest_at:
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(
                situation.last_telegram_digest_at.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            interval = telegram_cfg.get("digest_interval_hours", 2)
            if (now - last).total_seconds() >= interval * 3600:
                return True
        except Exception:
            return True

    return False
