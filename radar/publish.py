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
# 企业微信推送（群机器人 Webhook）
#
# 格式：news 图文卡片（公众号风格长方形链接）
# description 上限 ~500 字节（WeCom API 限制）
# 使用 unicode 分隔符 + emoji 构建视觉层次
# ================================================================

import re as _re
import asyncio as _asyncio

def _sanitize_wecom_md(text: str) -> str:
    """清理 WeCom 不支持的语法: backtick → 书名号，移除水平线"""
    text = _re.sub(r'`([^`]+)`', r'《\1》', text)
    text = _re.sub(r'\n---+\n', '\n', text)
    text = _re.sub(r'^---+$', '', text, flags=_re.MULTILINE)
    return text


def _get_wecom_env() -> str:
    return os.environ.get("WECOM_WEBHOOK_URL", "")


# —— 底层 POST ——

async def _wecom_post(webhook_url: str, msgtype: str, data: dict) -> bool:
    """发送一条 webhook 请求，含重试"""
    payload = {"msgtype": msgtype, msgtype: data}

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
                result = resp.json()
                if result.get("errcode") == 0:
                    return True
                else:
                    logger.error(f"WeCom API error (attempt {attempt + 1}): {result}")
                    if attempt < 2:
                        await _asyncio.sleep(2)
        except Exception as e:
            logger.error(f"WeCom request failed (attempt {attempt + 1}): {e}")
            if attempt < 2:
                await _asyncio.sleep(2)
    return False


# —— 公众号式图文卡片（news 类型） ——

async def send_wecom_news(articles: list[dict]) -> bool:
    """
    发送图文卡片（公众号风格），最多 8 条。
    articles: [{"title": "...", "url": "...", "description": "...", "picurl": "..."}]
    """
    webhook_url = _get_wecom_env()
    if not webhook_url:
        logger.warning("WECOM_WEBHOOK_URL not set, skipping WeCom push")
        return False

    articles = articles[:8]
    if not articles:
        return False

    for a in articles:
        a["title"] = _sanitize_wecom_md(a.get("title", "")[:120])
        # WeCom news description 上限 ~512 字节，按字节截断
        desc = a.get("description", "")
        desc_encoded = desc.encode("utf-8")[:500]
        a["description"] = _sanitize_wecom_md(desc_encoded.decode("utf-8", errors="ignore"))
        a.pop("picurl", None)

    return await _wecom_post(webhook_url, "news", {"articles": articles})


# —— 企业微信推送专用格式化 ——

def _fmt_tickers_wecom(tickers: list[str], max_display: int = 6) -> str:
    """紧凑标的展示"""
    if not tickers:
        return ""
    display = tickers[:max_display]
    s = ", ".join(display)
    if len(tickers) > max_display:
        s += f" +{len(tickers) - max_display}"
    return f" [{s}]"



def _fmt_time_hkt(iso_str: str | None) -> str:
    """ISO8601 → HH:MM HKT，跨天加日期前缀"""
    if not iso_str:
        return ""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        hkt = ZoneInfo("Asia/Hong_Kong")
        dt_hkt = dt.astimezone(hkt)
        now_hkt = datetime.now(hkt)
        if dt_hkt.date() == now_hkt.date():
            return dt_hkt.strftime("%H:%M")
        else:
            return dt_hkt.strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _extract_event_time(ev: Event, event_items: list[Item]) -> str:
    """从事件关联条目中提取最早发布时间"""
    best = ev.first_seen_at or ev.last_updated_at or ""
    for it in event_items:
        if it.published_at and (not best or it.published_at < best):
            best = it.published_at
    return _fmt_time_hkt(best)


def _aggregate_directions(items: list[Item]) -> dict[str, dict[str, int]]:
    """聚合方向信号: {ticker: {positive: N, negative: N, neutral: N}}"""
    agg: dict[str, dict[str, int]] = {}
    for it in items:
        d = it.direction if isinstance(it.direction, dict) else {}
        for tk, val in d.items():
            if tk not in agg:
                agg[tk] = {"positive": 0, "negative": 0, "neutral": 0}
            if val in agg[tk]:
                agg[tk][val] += 1
            else:
                agg[tk]["neutral"] += 1
    return agg


# —— 图文卡片格式化（公众号风格长方形链接） ——

def _importance_icon(sig: int) -> str:
    """重要性 emoji: 🔥高 ⚡中 ➤低"""
    if sig >= 8:
        return "🔥"
    elif sig >= 6:
        return "⚡"
    else:
        return "➤"


def _dir_icon(d: str) -> str:
    if d == "positive":
        return "↑"
    elif d == "negative":
        return "↓"
    return "→"


def _event_time(ev: Event, items_by_event: dict[str, list[Item]]) -> str:
    """事件时间（HKT），用于展示时效性"""
    return _extract_event_time(ev, items_by_event.get(ev.event_id, []))


def format_wecom_alert(
    new_events: list[Event],
    updated_events: list[Event],
    all_active_events: list[Event],
    situation: Optional[Situation],
    site_url: str = "",
    items: list[Item] | None = None,
    max_new_events: int = 6,
) -> dict:
    """格式化企业微信推送，返回单条 news 图文卡片

    卡片格式:
      ━━ 今日态势 ━━━━━━━━━━
      {态势综述}

      ▎新增 · N
        {icon} {title}  {time}  ↑N/10
        {summary}
        [{tickers}]  信源N  {deep_analysis}

      ▎更新 · N
        ...

      ▎洞察
        ▸趋势: {trend_spotting}
        ▸交叉: {cross_analysis}

      ━━ 活跃N  演进N  新增N  更新N ━━
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        hkt = ZoneInfo("Asia/Hong_Kong")
    except Exception:
        hkt = None
    now_dt = datetime.now(hkt) if hkt else datetime.now()
    now_str = now_dt.strftime("%m月%d日 %H:%M HKT") if hkt else ""

    card_title = f"AI 投研雷达 · {now_str}"

    # 去重
    updated_ids = {ev.event_id for ev in updated_events}
    deduped_new = [ev for ev in new_events if ev.event_id not in updated_ids]
    new_ids = {ev.event_id for ev in new_events}
    deduped_upd = [ev for ev in updated_events if ev.event_id not in new_ids]

    # Item → Event 索引（用于提取事件时间）
    items_by_event: dict[str, list[Item]] = {}
    if items:
        for it in items:
            if it.event_id:
                items_by_event.setdefault(it.event_id, []).append(it)

    # 统计
    active_count = len([e for e in all_active_events if e.is_active])
    developing = len([e for e in all_active_events if e.is_active and e.status == "developing"])

    desc_lines: list[str] = []

    # ━━ 态势 ━━
    if situation and situation.text:
        desc_lines.append("━━ 今日态势 " + "━" * 10)
        desc_lines.append(_clip(situation.text, 140))

    # ▎新增
    if deduped_new:
        sorted_new = sorted(deduped_new, key=lambda e: e.significance, reverse=True)
        visible_new = [e for e in sorted_new if e.significance >= 5]
        if visible_new:
            desc_lines.append("")
            desc_lines.append(f"▎新增 · {len(deduped_new)}")
            for ev in visible_new[:max_new_events]:
                icon = _importance_icon(ev.significance)
                tickers_str = _fmt_tickers_wecom(ev.tickers, max_display=4)
                direction = ev.direction or {}
                first_tk = list(direction.keys())[0] if direction else ""
                first_d = direction.get(first_tk, "") if first_tk else ""
                dir_str = _dir_icon(first_d)
                time_str = _event_time(ev, items_by_event)
                # 标题行: icon + title + time + score
                desc_lines.append(
                    f"  {icon} {_clip(ev.title or '', 22)}  {time_str}  {dir_str}{ev.significance}/10"
                )
                # 摘要行
                if ev.summary:
                    desc_lines.append(f"  {_clip(ev.summary, 28)}")
                # 元数据行: tickers + 信源 + deep_analysis
                meta = f"  {tickers_str}  信源{ev.source_count}"
                if ev.deep_analysis:
                    meta += f"  {_clip(ev.deep_analysis, 30)}"
                desc_lines.append(meta)
            remaining = len(visible_new) - max_new_events
            if remaining > 0:
                desc_lines.append(f"  +{remaining} 更多...")

    # ▎更新
    if deduped_upd:
        sorted_upd = sorted(deduped_upd, key=lambda e: e.significance, reverse=True)
        visible_upd = [e for e in sorted_upd if e.significance >= 5]
        if visible_upd:
            desc_lines.append("")
            desc_lines.append(f"▎更新 · {len(deduped_upd)}")
            for ev in visible_upd[:min(len(visible_upd), 3)]:
                icon = _importance_icon(ev.significance)
                tickers_str = _fmt_tickers_wecom(ev.tickers, max_display=4)
                direction = ev.direction or {}
                first_tk = list(direction.keys())[0] if direction else ""
                first_d = direction.get(first_tk, "") if first_tk else ""
                dir_str = _dir_icon(first_d)
                time_str = _event_time(ev, items_by_event)
                desc_lines.append(
                    f"  {icon} {_clip(ev.title or '', 22)}  {time_str}  {dir_str}{ev.significance}/10"
                )
                if ev.summary:
                    desc_lines.append(f"  {_clip(ev.summary, 28)}")
                meta = f"  {tickers_str}  信源{ev.source_count}"
                if ev.deep_analysis:
                    meta += f"  {_clip(ev.deep_analysis, 30)}"
                desc_lines.append(meta)
            remaining = len(visible_upd) - 3
            if remaining > 0:
                desc_lines.append(f"  +{remaining} 更多...")

    # ▎洞察
    insight_parts = []
    if situation and situation.trend_spotting:
        trend = _clip(situation.trend_spotting.strip(), 80)
        if trend:
            insight_parts.append(f"▸趋势: {trend}")
    if situation and situation.cross_analysis:
        cross = _clip(situation.cross_analysis.strip(), 120)
        if cross:
            insight_parts.append(f"▸交叉: {cross}")
    if insight_parts:
        desc_lines.append("")
        desc_lines.append("▎洞察")
        desc_lines.extend(insight_parts)

    # ▎当前关注（无新增/更新时展示活跃事件 TOP 5）
    if not deduped_new and not deduped_upd and all_active_events:
        top_active = sorted(
            [e for e in all_active_events if e.is_active and e.significance >= 5],
            key=lambda e: e.significance, reverse=True,
        )[:5]
        if top_active:
            desc_lines.append("")
            desc_lines.append("▎目前关注")
            for ev in top_active:
                icon = _importance_icon(ev.significance)
                tickers_str = _fmt_tickers_wecom(ev.tickers, max_display=4)
                direction = ev.direction or {}
                first_tk = list(direction.keys())[0] if direction else ""
                first_d = direction.get(first_tk, "") if first_tk else ""
                dir_str = _dir_icon(first_d)
                time_str = _event_time(ev, items_by_event)
                desc_lines.append(
                    f"  {icon} {_clip(ev.title or '', 22)}  {time_str}  {dir_str}{ev.significance}/10"
                )
                if ev.summary:
                    desc_lines.append(f"  {_clip(ev.summary, 28)}")
                meta = f"  {tickers_str}  信源{ev.source_count}"
                if ev.deep_analysis:
                    meta += f"  {_clip(ev.deep_analysis, 30)}"
                desc_lines.append(meta)

    # ━━ 底部统计 ━━
    stats = f"活跃{active_count}  演进{developing}"
    if deduped_new:
        stats += f"  新增{len(deduped_new)}"
    if deduped_upd:
        stats += f"  更新{len(deduped_upd)}"
    desc_lines.append("")
    desc_lines.append("━━ " + stats + " " + "━" * 4)

    description = "\n".join(desc_lines)

    return {
        "title": card_title,
        "description": description,
        "url": site_url,
    }


async def send_wecom_brief(
    title: str,
    brief_md: str,
    issue_url: str = "",
    site_url: str = "",
) -> bool:
    """推送晨报到企业微信: 单条 news 图文卡片（公众号风格长方形链接）

    提取晨报首段作为卡片描述，点击跳转完整 Issue。
    """
    # 提取首段非标题行作为描述
    desc = ""
    for line in brief_md.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith(">"):
            desc = _clip(line, 280)
            break

    return await send_wecom_news([{
        "title": title,
        "description": desc or "点击查看完整晨报",
        "url": issue_url or site_url,
    }])


def should_wecom_alert(
    new_events: list[Event],
    updated_events: list[Event],
    situation: Optional[Situation],
    cfg: dict,
) -> bool:
    """判断是否需要推送企业微信"""
    wecom_cfg = cfg.get("channels", {}).get("wecom", {})

    threshold = wecom_cfg.get("notify_new_event_threshold", 7)
    for ev in new_events:
        if ev.significance >= threshold:
            return True

    notify_update = wecom_cfg.get("notify_direction_flip", True)
    if notify_update and updated_events:
        for ev in updated_events:
            if ev.significance >= threshold:
                return True

    # 兜底推送间隔
    if situation and situation.last_wecom_digest_at:
        try:
            from datetime import datetime, timezone
            last = datetime.fromisoformat(
                situation.last_wecom_digest_at.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            interval = wecom_cfg.get("digest_interval_hours", 2)
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
