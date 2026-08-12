"""Telegram 版式渲染 —— DigestPayload → HTML 消息(单条, ≤4096 字符)

与 WeCom 同一版式理念: 零元数据、自然段落、加粗仅用于章节标题。
日报全文约 600-800 字, 可直接放进一条 TG 消息。
"""

import html
import logging

from radar.notify.types import DigestPayload, KIND_BREAKING

logger = logging.getLogger(__name__)

MAX_CHARS = 4096

_DEFAULT_BRAND = {
    "institute": "观澜研究院",
    "analyst": "沈砚舟",
    "analyst_title": "TMT 首席分析师",
}


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def render(payload: DigestPayload, site_url: str = "", issue_url: str = "",
           brand: dict | None = None) -> str:
    """渲染为单条 Telegram HTML 消息, 超预算时从尾部丢弃段落"""
    brand = brand or _DEFAULT_BRAND
    parts: list[str] = []

    # 报头
    if payload.kind == KIND_BREAKING:
        parts.append(f"<b>{_esc(payload.title)}</b>")
    else:
        p = payload.title.split("|", 1)
        product = p[0].strip()
        when = p[1].strip() if len(p) > 1 else ""
        parts.append(f"<b>{_esc(brand.get('institute', ''))} | {_esc(product)}</b>")
        byline = " · ".join(b for b in [when, brand.get("analyst", "")] if b)
        if byline:
            parts.append(_esc(byline))
    if payload.headline:
        parts.append(f"<i>{_esc(payload.headline)}</i>")

    # 正文
    for section in payload.sections:
        if section.is_empty():
            continue
        if section.heading.strip():
            parts.append(f"\n<b>{_esc(section.heading.strip())}</b>")
        for para in section.paragraphs:
            if para.strip():
                parts.append(_esc(para.strip()))
        for item in section.items:
            parts.append(f"<b>{_esc(item.title)}</b>")
            if item.summary:
                parts.append(_esc(item.summary))

    link = issue_url or site_url
    if link:
        label = "完整版 · 事件线与数据附录" if issue_url else "实时看板"
        parts.append(f'\n<a href="{link}">{label}</a>')

    text = "\n".join(parts)
    if len(text) <= MAX_CHARS:
        return text

    # 截断: 从尾部逐段丢弃(保留报头与核心判断)
    lines = text.split("\n")
    while len(lines) > 3 and len("\n".join(lines)) > MAX_CHARS - 120:
        lines.pop(-2)
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 80] + "…"
    if link:
        text += f'\n<a href="{link}">完整版</a>'
    return text
