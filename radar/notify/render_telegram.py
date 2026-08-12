"""Telegram 版式渲染 —— DigestPayload → HTML 消息(单条, ≤4096 字符)

与 WeCom 同一版式理念: 零元数据、自然段落、加粗仅用于章节标题。
日报全文约 600-800 字, 可直接放进一条 TG 消息。
"""

import html
import logging

from radar.notify.assemble import sources_by_ref
from radar.notify.brand import (
    DEFAULT_BRAND as _DEFAULT_BRAND, alert_header, ordinal, source_label,
)
from radar.notify.types import DigestPayload, SHIFT_LABEL, KIND_BREAKING

logger = logging.getLogger(__name__)

MAX_CHARS = 4096


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _alert_parts(payload: DigestPayload, src_map: dict, issue_url: str) -> list[str]:
    """快报三段: 事件句 / 含义 / 盯什么, 末尾挂原文链接"""
    alert = payload.alert
    if alert is None:
        return []
    parts: list[str] = []
    if alert.summary.strip():
        parts.append(f"\n{_esc(alert.summary.strip())}")
    if alert.why.strip():
        parts.append(f"<b>含义</b> {_esc(alert.why.strip())}")
    if alert.watch.strip():
        parts.append(f"<b>盯</b> {_esc(alert.watch.strip())}")

    tail = []
    for src in (src_map.get(alert.evidence_ref) or [])[:1]:
        if src.get("url"):
            bits = [b for b in [src.get("source", ""), source_label(src)] if b]
            link = f'<a href="{src["url"]}">原文</a>'
            tail.append(link + (f" · {_esc(' · '.join(bits))}" if bits else ""))
    if issue_url:
        tail.append(f'<a href="{issue_url}">今日快报汇总</a>')
    if tail:
        parts.append("\n" + " · ".join(tail))
    return parts


def _call_parts(payload: DigestPayload, src_map: dict) -> list[str]:
    """判断链的 TG 形态: 判断 / 推论 / 证伪, 每条判断挂一个证据链接

    TG 支持 inline <a>, 一条链接不影响版面, 所以比微信多给一个来源入口。
    """
    parts: list[str] = []

    macro = payload.macro
    if macro is not None and not macro.is_empty():
        parts.append("\n<b>格局</b>")
        if macro.cycle:
            parts.append(f"周期位置：{_esc(macro.cycle)}")
        traj = macro.trajectory
        if not traj.is_empty():
            arc = " → ".join(s for s in [traj.past, traj.now, traj.next] if s)
            parts.append(f"主线轨迹：{_esc(arc)}")
            parts.append(f"当下：<b>{_esc(traj.now)}</b>")
            if traj.trigger:
                parts.append(f"切换信号：{_esc(traj.trigger)}")
        if macro.shift:
            label = SHIFT_LABEL.get(macro.shift_kind, "")
            parts.append(f"较上期：{_esc((label + ' — ' if label else '') + macro.shift)}")

    for call in payload.calls:
        parts.append(f"\n<b>判断{ordinal(call.n)} · {_esc(call.claim)}</b>")
        body = call.inference.strip() or call.fact.strip()
        if body:
            parts.append(_esc(body))
        if call.falsifier.strip():
            parts.append(f"证伪：{_esc(call.falsifier.strip())}")
        for ref in call.evidence_refs:
            srcs = src_map.get(ref) or []
            if srcs and srcs[0].get("url"):
                title = _esc(srcs[0].get("title", "") or "来源")
                parts.append(f'证据：<a href="{srcs[0]["url"]}">{title}</a>')
                break

    if payload.tension.strip():
        parts.append("\n<b>张力与联动</b>")
        parts.append(_esc(payload.tension.strip()))

    if payload.reviews:
        parts.append("\n<b>上期回溯</b>")
        for rv in payload.reviews:
            parts.append(f"{_esc(rv.claim)} — {_esc(rv.status)}")

    return parts


def render(payload: DigestPayload, site_url: str = "", issue_url: str = "",
           brand: dict | None = None, material: dict | None = None) -> str:
    """渲染为单条 Telegram HTML 消息, 超预算时从尾部丢弃段落"""
    brand = brand or _DEFAULT_BRAND
    src_map = sources_by_ref(material or {})
    parts: list[str] = []

    # 报头
    if payload.kind == KIND_BREAKING:
        p = payload.title.split("|", 1)
        product = p[0].strip() or "首席快报"
        when = p[1].strip() if len(p) > 1 else ""
        title, *rest = alert_header(material or {}, brand, product, when)
        parts.append(f"<b>{_esc(title)}</b>")
        parts.extend(_esc(line) for line in rest)
    else:
        p = payload.title.split("|", 1)
        product = p[0].strip()
        when = p[1].strip() if len(p) > 1 else ""
        parts.append(f"<b>{_esc(brand.get('institute', ''))} | {_esc(product)}</b>")
        byline = " · ".join(b for b in [when, brand.get("analyst", "")] if b)
        if byline:
            parts.append(_esc(byline))
    if payload.fallback:
        parts.append("（本期为降级稿：撰稿环节未完成，仅事实层）")
    if payload.headline:
        parts.append(f"<i>{_esc(payload.headline)}</i>")

    parts.extend(_alert_parts(payload, src_map, issue_url))
    parts.extend(_call_parts(payload, src_map))

    # 正文(快讯/复盘走通用段落形态)
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

    # 快报的链接已由 _alert_parts 放在正文末尾, 不重复
    link = issue_url if payload.kind != KIND_BREAKING else ""
    if link:
        parts.append(f'\n<a href="{link}">完整版 · 事件线与数据附录</a>')

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
