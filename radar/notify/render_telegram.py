"""Telegram 版式渲染 —— DigestPayload → HTML 消息(单条, ≤4096 字符)

审计 M4: Telegram 版不增加 LLM 调用 —— 晨报 = headline + 第一 section(核心观点)截取,
快讯/速递 = 完整内容。弃用 legacy Markdown(对中文标点/特殊字符脆弱), 改用 HTML。
"""

import html
import logging

from radar.notify.types import DigestPayload, KIND_MORNING, KIND_BREAKING

logger = logging.getLogger(__name__)

MAX_CHARS = 4096


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _sig_icon(sig: int) -> str:
    if sig >= 8:
        return "🔥"
    if sig >= 6:
        return "⚡"
    return "➤"


def _render_full(payload: DigestPayload) -> str:
    """快讯/速递: 全量渲染"""
    parts: list[str] = []
    if payload.title:
        parts.append(f"<b>{_esc(payload.title)}</b>")
    if payload.headline:
        parts.append(f"<i>{_esc(payload.headline)}</i>")
    for section in payload.sections:
        if section.is_empty():
            continue
        if section.heading:
            parts.append(f"\n<b>{_esc(section.heading)}</b>")
        for para in section.paragraphs:
            if para.strip():
                parts.append(_esc(para.strip()))
        for item in section.items:
            icon = _sig_icon(item.significance)
            tickers = f" [{', '.join(item.tickers[:5])}]" if item.tickers else ""
            parts.append(f"{icon} <b>{_esc(item.title)}</b>{_esc(tickers)} {item.significance}/10")
            if item.summary:
                parts.append(_esc(item.summary))
            if item.why:
                parts.append(f"影响: {_esc(item.why)}")
            if item.watch:
                parts.append(f"关注: {_esc(item.watch)}")
    return "\n".join(parts)


def _render_morning_brief(payload: DigestPayload, link: str) -> str:
    """晨报: headline + 核心观点 + 链接(完整版在 Issue)"""
    parts: list[str] = []
    if payload.title:
        parts.append(f"<b>{_esc(payload.title)}</b>")
    if payload.headline:
        parts.append(f"<i>{_esc(payload.headline)}</i>")
    # 只取第一个段落型 section(核心观点)
    for section in payload.sections:
        if section.paragraphs:
            parts.append(f"\n<b>{_esc(section.heading)}</b>")
            for para in section.paragraphs:
                if para.strip():
                    parts.append(_esc(para.strip()))
            break
    if link:
        parts.append(f'\n<a href="{link}">完整晨报</a>')
    return "\n".join(parts)


def render(payload: DigestPayload, site_url: str = "", issue_url: str = "") -> str:
    """渲染为单条 Telegram HTML 消息, 超预算时截断段落"""
    link = issue_url or site_url
    if payload.kind == KIND_MORNING:
        text = _render_morning_brief(payload, link)
    else:
        text = _render_full(payload)
        if link:
            label = "完整晨报" if issue_url else "实时看板"
            text += f'\n<a href="{link}">{label}</a>'

    if len(text) <= MAX_CHARS:
        return text

    # 截断: 逐段丢弃直到满足预算
    budget = MAX_CHARS - 80
    lines = text.split("\n")
    while lines and len("\n".join(lines)) > budget:
        # 从倒数第二段开始丢(保留标题)
        if len(lines) > 2:
            lines.pop(-2)
        else:
            break
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 60] + "…"
    if link:
        text += f'\n<a href="{link}">完整版</a>'
    return text
