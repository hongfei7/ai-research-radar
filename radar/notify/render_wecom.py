"""WeCom 版式渲染 —— DigestPayload → markdown 消息列表

版式理念(重构): 研报风 + 阅读友好
- 零元数据: 无评分/箭头/标的括号/信源数 —— 判断融在文字里
- 零复杂排版: 无引用块嵌套、无 emoji 堆砌; 层级只有"加粗章节标题 + 自然段"
- 不用 # 标题 / backtick / 表格(WeCom 支持不完整)
- 单条 ≤3800 字节, 按 block(标题/段落)边界拆分, 杜绝句中截断
"""

import logging

from radar.notify.types import DigestPayload, DigestSection, KIND_BREAKING

logger = logging.getLogger(__name__)

MAX_BYTES = 3800

_DEFAULT_BRAND = {
    "institute": "Sterling 证券研究",
    "analyst": "张灏铭 Haoming Zhang",
    "analyst_title": "TMT 首席分析师",
}


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


def _item_block(item) -> list[str]:
    """兜底稿件的条目渲染: 仅加粗标题 + 摘要, 无元数据"""
    lines = [f"**{item.title}**"]
    if item.summary:
        lines.append(item.summary)
    return lines


def render(payload: DigestPayload, site_url: str = "", issue_url: str = "",
           brand: dict | None = None) -> list[str]:
    """渲染为 WeCom markdown 消息列表(按 block 边界拆分)"""
    brand = brand or _DEFAULT_BRAND

    # —— 组装 block 序列: 每个标题/段落/条目一个 block ——
    blocks: list[list[str]] = []

    # 报头: 机构 | 产品 换行 日期 · 署名
    header = []
    if payload.kind == KIND_BREAKING:
        header.append(f"**{payload.title}**")
    else:
        # title 形如 "AI 首席内参 | 08月12日 07:00"
        parts = payload.title.split("|", 1)
        product = parts[0].strip()
        when = parts[1].strip() if len(parts) > 1 else ""
        header.append(f"**{brand.get('institute', '')} | {product}**".replace("** ", "**").strip())
        byline_bits = [b for b in [when, brand.get("analyst", "")] if b]
        if byline_bits:
            header.append(" · ".join(byline_bits))
    if header:
        blocks.append(header)
    if payload.headline:
        blocks.append([payload.headline])

    for section in payload.sections:
        if section.is_empty():
            continue
        if section.heading.strip():
            blocks.append([f"**{section.heading.strip()}**"])
        for para in section.paragraphs:
            if para.strip():
                blocks.append([para.strip()])
        for item in section.items:
            blocks.append(_item_block(item))

    # 报尾: 仅一条完整版链接
    link = issue_url or site_url
    if link:
        label = "完整版 · 事件线与数据附录" if issue_url else "实时看板"
        blocks.append([f"[{label}]({link})"])

    # —— 贪心装包: block 按序装入消息, 不超 MAX_BYTES; block 间空行分隔 ——
    cont_title = f"**{payload.title}(续)**" if payload.title else ""
    messages: list[str] = []
    cur: list[list[str]] = []

    def _cur_bytes() -> int:
        return len("\n\n".join("\n".join(b) for b in cur).encode("utf-8"))

    def _flush():
        nonlocal cur
        if cur:
            messages.append("\n\n".join("\n".join(b) for b in cur))
            cur = []

    for block in blocks:
        sub_blocks = [block] if _blocks_bytes(block) <= MAX_BYTES - 100 else \
            _split_oversize_block(block, MAX_BYTES - 100)
        for sb in sub_blocks:
            overhead = (len(cont_title.encode("utf-8")) + 2) if messages else 0
            if cur and _cur_bytes() + 2 + _blocks_bytes(sb) > MAX_BYTES - overhead:
                _flush()
                if cont_title:
                    cur.append([cont_title])
            cur.append(sb)
    _flush()

    return messages
