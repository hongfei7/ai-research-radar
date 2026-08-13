"""WeCom 版式渲染 —— DigestPayload → markdown 消息列表

版式理念(重构): 研报风 + 阅读友好
- 零元数据: 无评分/箭头/标的括号/信源数 —— 判断融在文字里
- 零复杂排版: 无引用块嵌套、无 emoji 堆砌; 层级只有"加粗章节标题 + 自然段"
- 不用 # 标题 / backtick / 表格(WeCom 支持不完整)
- 单条 ≤3800 字节, 按 block(标题/段落)边界拆分, 杜绝句中截断
"""

import logging

from radar.notify.assemble import sources_by_ref
from radar.notify.brand import (
    DEFAULT_BRAND as _DEFAULT_BRAND, alert_header, caveat_label, ordinal,
    publisher_name,
)
from radar.notify.types import DigestPayload, DigestSection, SHIFT_LABEL, KIND_BREAKING

logger = logging.getLogger(__name__)

MAX_BYTES = 3800

# 企业微信 markdown(v1) 只支持: 标题 / 加粗 / 链接 / 行内代码 / 引用 /
# 三种字体色(info|comment|warning)。分割线、列表、表格都不渲染 ——
# 之前用的 "---" 会原样显示成三个减号, 还把手机通知栏的预览位置占掉。
# 分割线与列表只在 markdown_v2, 但 v2 不支持字体色, 会丢掉"低可信"的灰字降权。
# 所以层级一律靠空行与加粗前缀表达。
# https://developer.work.weixin.qq.com/document/path/91770


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


def _call_blocks(payload: DigestPayload) -> list[list[str]]:
    """判断链的微信形态

    微信里读长文很痛苦, 所以只带 判断 / 推论 / 证伪三段, 略去事实与机理 ——
    要看完整论证与证据链接就点 Issue。证据链接一律不进微信, 一条消息塞四五个
    链接会把版面冲垮。
    """
    blocks: list[list[str]] = []

    macro = payload.macro
    if macro is not None and not macro.is_empty():
        macro_lines = ["**格局**"]
        if macro.cycle:
            macro_lines.append(f"周期位置：{macro.cycle}")
        traj = macro.trajectory
        if not traj.is_empty():
            arc = " → ".join(s for s in [traj.past, traj.now, traj.next] if s)
            macro_lines.append(f"主线轨迹：{arc}（当下：{traj.now}）")
            if traj.trigger:
                macro_lines.append(f"切换信号：{traj.trigger}")
        if macro.shift:
            label = SHIFT_LABEL.get(macro.shift_kind, "")
            macro_lines.append(f"较上期：{label + ' —— ' if label else ''}{macro.shift}")
        blocks.append(macro_lines)

    for call in payload.calls:
        lines = [f"**判断{ordinal(call.n)} · {call.claim}**"]
        if call.inference.strip():
            lines.append(call.inference.strip())
        elif call.fact.strip():
            lines.append(call.fact.strip())
        if call.falsifier.strip():
            lines.append(f"证伪：{call.falsifier.strip()}")
        blocks.append(lines)

    if payload.tension.strip():
        blocks.append(["**张力与联动**", payload.tension.strip()])

    if payload.reviews:
        review_lines = ["**上期回溯**"]
        for rv in payload.reviews:
            review_lines.append(f"{rv.claim} —— {rv.status}")
        blocks.append(review_lines)

    return blocks


def _alert_blocks(payload: DigestPayload, material: dict, issue_url: str) -> list[list[str]]:
    """快报正文: 事实 / 判断 / 盯什么 三段, 末尾一行报尾"""
    alert = payload.alert
    if alert is None:
        return []
    blocks: list[list[str]] = []
    if alert.summary.strip():
        blocks.append([alert.summary.strip()])
    if alert.why.strip():
        blocks.append([f"**判断** {alert.why.strip()}"])
    if alert.watch.strip():
        blocks.append([f"**盯** {alert.watch.strip()}"])

    tail = []
    sources = sources_by_ref(material or {}).get(alert.evidence_ref, [])
    for src in sources[:1]:
        if src.get("url"):
            bits = [f"[原文]({src['url']})"]
            attribution = publisher_name(src)
            # 交叉验证强度本身就是信号(它已计入 significance 的多源加成),
            # 却在推送里完全看不见; 单源也更容易被一家媒体的转述错误带偏
            if len(sources) > 1:
                attribution = f"{attribution} +{len(sources) - 1}家".strip()
            if attribution:
                bits.append(attribution)
            caveat = caveat_label(src)
            if caveat:
                # 可信度是脚注不是正文, 压成灰字以免和判断抢注意力
                bits.append(f'<font color="comment">{caveat}</font>')
            tail.append(" · ".join(bits))
    if issue_url:
        tail.append(f"[今日汇总]({issue_url})")
    if tail:
        blocks.append([" · ".join(tail)])
    return blocks


def render(payload: DigestPayload, site_url: str = "", issue_url: str = "",
           brand: dict | None = None, material: dict | None = None) -> list[str]:
    """渲染为 WeCom markdown 消息列表(按 block 边界拆分)"""
    brand = brand or _DEFAULT_BRAND

    # —— 组装 block 序列: 每个标题/段落/条目一个 block ——
    blocks: list[list[str]] = []

    # 报头: 机构 | 产品 换行 日期 · 署名
    header = []
    if payload.kind == KIND_BREAKING:
        parts = payload.title.split("|", 1)
        product = parts[0].strip() or "首席快报"
        when = parts[1].strip() if len(parts) > 1 else ""
        title, *rest = alert_header(material or {}, brand, product, when)
        header.append(f"**{title}**")
        header.extend(rest)
    else:
        # title 形如 "AI 首席内参 | 08月12日 07:00"
        parts = payload.title.split("|", 1)
        product = parts[0].strip()
        when = parts[1].strip() if len(parts) > 1 else ""
        header.append(f"**{brand.get('institute', '')} | {product}**".replace("** ", "**").strip())
        byline_bits = [b for b in [when, brand.get("analyst", "")] if b]
        if byline_bits:
            header.append(" · ".join(byline_bits))
    if payload.fallback:
        header.append("（本期为降级稿：撰稿环节未完成，仅事实层）")
    if header:
        blocks.append(header)

    body: list[list[str]] = []
    if payload.headline:
        body.append([payload.headline])

    body.extend(_alert_blocks(payload, material or {}, issue_url))
    body.extend(_call_blocks(payload))

    for section in payload.sections:
        if section.is_empty():
            continue
        if section.heading.strip():
            body.append([f"**{section.heading.strip()}**"])
        for para in section.paragraphs:
            if para.strip():
                body.append([para.strip()])
        for item in section.items:
            body.append(_item_block(item))

    blocks.extend(body)

    # 报尾: 仅完整版 Issue 链接(实时看板已废弃, 报告唯一完整载体为 Issue)
    # 快报的链接已由 _alert_blocks 放在正文末尾, 不重复
    if issue_url and payload.kind != KIND_BREAKING:
        blocks.append([f"[完整版 · 事件线与数据附录]({issue_url})"])

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
