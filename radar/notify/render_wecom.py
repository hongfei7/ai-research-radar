"""WeCom 版式渲染 —— DigestPayload → markdown 消息列表

版式规范(继承现有实证, 审计 M5d):
- 不用 # 标题 / 不用 backtick / 不用表格(WeCom 支持不完整)
- 层级: **加粗** 标题 + > 引用块 + <font color> 三色(info/comment/warning)
- 单条消息 ≤3800 字节(4096 留 margin), 按 block 边界拆分, 杜绝句中截断
- 单 block 超限时按行二次拆分并加 "(续)" 标记
"""

import logging

from radar.notify.types import DigestPayload, DigestSection, DigestItem, KIND_BREAKING

logger = logging.getLogger(__name__)

MAX_BYTES = 3800

_SECTION_NUMERALS = ["一", "二", "三", "四", "五", "六", "七", "八"]


def _sig_icon(sig: int) -> str:
    if sig >= 8:
        return "🔥"
    if sig >= 6:
        return "⚡"
    return "➤"


def _dir_arrow(d: str) -> str:
    return {"positive": "↑", "negative": "↓", "neutral": "→", "mixed": "↕"}.get(d, "")


def _fmt_tickers(tickers: list, max_display: int = 5) -> str:
    if not tickers:
        return ""
    s = ", ".join(str(t) for t in tickers[:max_display])
    if len(tickers) > max_display:
        s += f" +{len(tickers) - max_display}"
    return f" [{s}]"


def _item_block(item: DigestItem, verbose: bool) -> list[str]:
    """单个事件条目的引用块(行列表)"""
    icon = _sig_icon(item.significance)
    arrow = _dir_arrow(item.direction)
    meta = f" {arrow}{item.significance}/10" if arrow else f" {item.significance}/10"
    lines = [f"> {icon} **{item.title}**{_fmt_tickers(item.tickers)}{meta}"]
    if item.summary:
        lines.append(f"> {item.summary}")
    if verbose and item.why:
        lines.append(f"> 影响: {item.why}")
    if verbose and item.watch:
        lines.append(f'> <font color="comment">关注: {item.watch}</font>')
    return lines


def _section_blocks(section: DigestSection, index: int, verbose: bool) -> list[list[str]]:
    """一个 section 拆成若干 block: 标题 block + 每段/每条目一个 block"""
    blocks: list[list[str]] = []
    numeral = _SECTION_NUMERALS[index] if index < len(_SECTION_NUMERALS) else str(index + 1)
    heading = section.heading.strip()
    if heading:
        blocks.append([f"**{numeral}、{heading}**"])
    for para in section.paragraphs:
        if para.strip():
            blocks.append([para.strip()])
    for item in section.items:
        blocks.append(_item_block(item, verbose))
    return blocks


def _blocks_bytes(block: list[str]) -> int:
    return len("\n".join(block).encode("utf-8"))


def _split_oversize_block(block: list[str], budget: int) -> list[list[str]]:
    """单 block 超预算时按行二次拆分"""
    parts: list[list[str]] = []
    cur: list[str] = []
    for line in block:
        if cur and _blocks_bytes(cur + [line]) > budget:
            parts.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        parts.append(cur)
    # 单行之内的极端超限: 字节级截断(兜底, 为省略号预留 8 字节)
    fixed: list[list[str]] = []
    for part in parts:
        if part and len(part[0].encode("utf-8")) > budget:
            raw = part[0].encode("utf-8")[:budget - 8]
            part[0] = raw.decode("utf-8", errors="ignore") + "…"
        fixed.append(part)
    return fixed


def render(payload: DigestPayload, site_url: str = "", issue_url: str = "") -> list[str]:
    """渲染为 WeCom markdown 消息列表(按 block 边界拆分)"""
    verbose = payload.kind != KIND_BREAKING  # 快讯已含 why/watch, 晨报/速递全字段

    # —— 组装 block 序列 ——
    blocks: list[list[str]] = []
    header = [f"**{payload.title}**"] if payload.title else []
    if payload.headline:
        header.append(f'<font color="info">{payload.headline}</font>')
    if header:
        blocks.append(header)

    for i, section in enumerate(payload.sections):
        if section.is_empty():
            continue
        blocks.extend(_section_blocks(section, i, verbose))

    footer_lines = []
    if payload.footer:
        footer_lines.append(f'<font color="comment">{payload.footer}</font>')
    footer_lines.append('<font color="comment">仅作研究素材, 不构成投资建议</font>')
    link = issue_url or site_url
    if link:
        label = "完整晨报" if issue_url else "实时看板"
        footer_lines.append(f"[{label}]({link})")
    blocks.append(footer_lines)

    # —— 贪心装包: block 按序装入消息, 不超 MAX_BYTES ——
    cont_title = f"**{payload.title}(续)**" if payload.title else ""
    messages: list[str] = []
    cur: list[str] = []
    for block in blocks:
        # 单 block 超限先二次拆分
        sub_blocks = [block] if _blocks_bytes(block) <= MAX_BYTES - 100 else \
            _split_oversize_block(block, MAX_BYTES - 100)
        for sb in sub_blocks:
            overhead = len((cont_title + "\n").encode("utf-8")) if messages else 0
            if cur and _blocks_bytes(cur) + 1 + _blocks_bytes(sb) > MAX_BYTES - overhead:
                messages.append("\n".join(cur))
                cur = [cont_title] if cont_title else []
            cur.extend(sb)
    if cur:
        messages.append("\n".join(cur))

    return messages
