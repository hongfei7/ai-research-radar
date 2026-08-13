"""内参正文渲染 —— DigestPayload → 完整 Markdown(GitHub Issue / reports 归档同稿)

版式为四层判断链:
  报头 → 格局(宏观) → 本期速览 → 上期回溯 → 判断一..N(事实/机理/推论/证伪/证据/反面)
  → 张力与联动(中观) → 未进入判断的观察 → 附录

证据链接由 ref 还原: LLM 只输出 E1/E2, 本层从素材的 sources 查表生成 markdown 链接,
因此报告里不可能出现幻觉链接。查不到的 ref 静默跳过, 宁可少给也不给死链。
"""

import logging

from radar.notify.assemble import sources_by_ref
from radar.notify.brand import (
    DEFAULT_BRAND, alert_header, brand_of, ordinal, publisher_name,
    source_label, short_date,
)
from radar.notify.types import DigestPayload, SHIFT_LABEL, KIND_BREAKING
from radar.textnorm import clean_title, strip_markdown

logger = logging.getLogger(__name__)

_MAX_APPENDIX_ROWS = 20
_MAX_EVIDENCE_PER_CALL = 4


def _md_escape_cell(text: str) -> str:
    """表格单元格: 去 markdown 记号并转义竖线, 否则一个 | 就能把整行表格打散"""
    return strip_markdown(text or "").replace("|", "／")


def _evidence_lines(refs: list, src_map: dict) -> list[str]:
    """把 ref 列表还原成带链接的证据行"""
    lines: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        for src in src_map.get(ref, []):
            url = (src.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = clean_title(src.get("title") or "") or url
            bits = [b for b in [
                publisher_name(src),
                short_date(src.get("published_at", "")),
                source_label(src),
            ] if b]
            lines.append(f"- [{title}]({url}) · {' · '.join(bits)}")
            if len(lines) >= _MAX_EVIDENCE_PER_CALL:
                return lines
    if not lines and refs:
        logger.warning(f"No resolvable sources for evidence refs: {refs}")
    return lines


def _header(payload: DigestPayload, brand: dict, material: dict) -> list[str]:
    product = payload.title.split("|", 1)[0].strip() if payload.title else "内参"
    when = payload.title.split("|", 1)[1].strip() if "|" in payload.title else ""

    if payload.kind == KIND_BREAKING:
        # 快报归档为当日汇总 Issue 里的一条, 外层已有 "### 时间" 小标题,
        # 再套 H1 报头会层级倒置; 这里只给品牌行与标的行
        title, *rest = alert_header(material or {}, brand, product, when)
        return [f"**{title}**", *rest, ""]

    lines = [f"# {brand.get('institute', '')} | {product}"]
    byline = " · ".join(b for b in [
        when, f"{brand.get('analyst', '')}({brand.get('analyst_title', '')})",
    ] if b)
    if byline:
        lines.append(f"*{byline}*")

    stats = (material or {}).get("stats") or {}
    window = (material or {}).get("window_hours")
    bits = []
    if window:
        bits.append(f"窗口 {int(window)}h")
    if stats.get("items_ingested") is not None:
        bits.append(f"{stats['items_ingested']} 条入库")
    if stats.get("events") is not None:
        bits.append(f"{stats['events']} 事件")
    if payload.calls:
        bits.append(f"{len(payload.calls)} 条进入判断")
    if bits:
        lines.append(f"*{' · '.join(bits)}*")
    if payload.fallback:
        lines.append("")
        lines.append("> **本期为降级稿**：撰稿环节未能完成，以下仅为事实层罗列，无机理与推论。")
    lines.append("")
    return lines


def _macro_block(payload: DigestPayload) -> list[str]:
    macro = payload.macro
    if macro is None or macro.is_empty():
        return []
    heading = "## 格局"
    if macro.shift_kind == "pivot":
        heading += "（框架转向）"
    lines = [heading, ""]
    if macro.cycle:
        lines.append(f"**周期位置** {macro.cycle}")
        lines.append("")

    # 主线轨迹: 过去 → 当下 → 未来, 当下一段加粗。缺失的段落用占位符,
    # 保持箭头结构完整 —— 断掉的箭头比"未知"更难读
    traj = macro.trajectory
    if not traj.is_empty():
        segments = [
            f"过去：{traj.past}" if traj.past else "过去：未标注",
            f"**当下：{traj.now}**",
        ]
        if traj.next:
            segments.append(f"未来：{traj.next}")
        lines.append(f"**主线轨迹** {' → '.join(segments)}")
        lines.append("")
        if traj.why_now:
            lines.append(traj.why_now)
            lines.append("")
        if traj.trigger:
            lines.append(f"**切换信号** {traj.trigger}")
            lines.append("")

    if macro.blindspot:
        lines.append(f"**盲区** {macro.blindspot}")
        lines.append("")
    if macro.check:
        lines.append(f"**本期检验** {macro.check}")
        lines.append("")

    if macro.shift:
        label = SHIFT_LABEL.get(macro.shift_kind, "")
        prefix = f"**较上期** {label} —— " if label else "**较上期** "
        lines.append(f"{prefix}{macro.shift}")
        lines.append("")
    return lines


def _overview_table(payload: DigestPayload) -> list[str]:
    # 降级稿没有验证点, 速览表会退化成一列空白, 不如不出
    if not payload.calls or not any(c.verify.strip() for c in payload.calls):
        return []
    lines = ["## 本期速览", "", "| # | 判断 | 方向 | 最快证伪/验证点 |", "|---|---|---|---|"]
    for call in payload.calls:
        lines.append(
            f"| {call.n} | {_md_escape_cell(call.claim)} | "
            f"{_md_escape_cell(call.direction)} | {_md_escape_cell(call.verify)} |"
        )
    lines.append("")
    return lines


def _reviews_block(payload: DigestPayload, src_map: dict) -> list[str]:
    if not payload.reviews:
        return []
    lines = ["## 上期判断回溯", "", "| 上期判断 | 证伪条件 | 状态 | 依据 |", "|---|---|---|---|"]
    for rv in payload.reviews:
        basis = _md_escape_cell(rv.basis)
        # 依据附上首个可解析的来源链接, 让回溯本身也可核对
        for ref in rv.evidence_refs:
            srcs = src_map.get(ref) or []
            if srcs and srcs[0].get("url"):
                basis = f"{basis} [来源]({srcs[0]['url']})"
                break
        lines.append(
            f"| {_md_escape_cell(rv.claim)} | {_md_escape_cell(rv.falsifier)} | "
            f"{rv.status} | {basis} |"
        )
    lines.append("")
    return lines


def _calls_block(payload: DigestPayload, src_map: dict) -> list[str]:
    lines: list[str] = []
    for call in payload.calls:
        lines.append("---")
        lines.append("")
        lines.append(f"## 判断{ordinal(call.n)} · {call.claim}")
        lines.append("")
        for label, text in (
            ("事实", call.fact), ("机理", call.mechanism),
            ("推论", call.inference), ("证伪条件", call.falsifier),
        ):
            if text.strip():
                lines.append(f"**{label}** {text.strip()}")
                lines.append("")
        evidence = _evidence_lines(call.evidence_refs, src_map)
        if evidence:
            lines.append("**证据**")
            lines.append("")
            lines.extend(evidence)
            lines.append("")
        if call.counterpoint.strip():
            lines.append(f"**反面** {call.counterpoint.strip()}")
            lines.append("")
    return lines


def _tension_block(payload: DigestPayload) -> list[str]:
    if not payload.tension.strip():
        return []
    return ["---", "", "## 张力与联动", "", payload.tension.strip(), ""]


def _watchlist_block(payload: DigestPayload, src_map: dict) -> list[str]:
    if not payload.watchlist:
        return []
    lines = ["## 未进入判断的观察", ""]
    for w in payload.watchlist:
        text = strip_markdown(w.get("text", ""))
        if not text:
            continue
        srcs = src_map.get(w.get("ref", "")) or []
        url = srcs[0].get("url") if srcs else ""
        lines.append(f"- {text} [链接]({url})" if url else f"- {text}")
    lines.append("")
    return lines


def _alert_block(payload: DigestPayload, src_map: dict) -> list[str]:
    """快报归档正文: 与推送同稿, 证据展开为完整清单"""
    alert = payload.alert
    if alert is None:
        return []
    lines: list[str] = []
    if alert.summary.strip():
        lines.append(alert.summary.strip())
        lines.append("")
    for label, text in (("判断", alert.why), ("盯", alert.watch)):
        if text.strip():
            lines.append(f"**{label}** {text.strip()}")
            lines.append("")
    evidence = _evidence_lines([alert.evidence_ref] if alert.evidence_ref else [], src_map)
    if evidence:
        lines.append("**证据**")
        lines.append("")
        lines.extend(evidence)
        lines.append("")
    return lines


def _sections_block(payload: DigestPayload) -> list[str]:
    """快讯/复盘仍走通用段落形态"""
    lines: list[str] = []
    for section in payload.sections:
        if section.is_empty():
            continue
        if section.heading.strip():
            lines.append(f"## {section.heading.strip()}")
            lines.append("")
        for para in section.paragraphs:
            if para.strip():
                lines.append(para.strip())
                lines.append("")
        for item in section.items:
            lines.append(f"**{item.title}**")
            lines.append("")
            if item.summary:
                lines.append(item.summary)
                lines.append("")
    return lines


def _appendix(payload: DigestPayload, material: dict) -> list[str]:
    # 单事件快报的证据清单已经把来源列全, 再来一张单行附录纯属重复
    if payload.kind == KIND_BREAKING:
        return []
    events = (material or {}).get("events") or []
    if not events:
        return []
    lines = [
        "---", "",
        "## 附录：事件线与数据", "",
        "| 事件 | 主标的 | 信源数 | 首报 | 链接 |",
        "|---|---|---|---|---|",
    ]
    for ev in events[:_MAX_APPENDIX_ROWS]:
        tickers = "、".join((ev.get("tickers") or [])[:3])
        # 报新闻何时发生, 不是爬虫何时看到
        first_seen = short_date(ev.get("first_published_at") or ev.get("first_seen_at", ""))
        srcs = ev.get("sources") or []
        link = f"[原文]({srcs[0]['url']})" if srcs and srcs[0].get("url") else "—"
        lines.append(
            f"| {_md_escape_cell(clean_title(ev.get('title', '')))} | {tickers or '—'} | "
            f"{ev.get('source_count', 0)} | {first_seen or '—'} | {link} |"
        )
    lines.append("")
    return lines


def render_issue_body(payload: DigestPayload, site_url: str = "",
                      material: dict | None = None,
                      brand: dict | None = None) -> str:
    brand = brand or DEFAULT_BRAND
    material = material or {}
    src_map = sources_by_ref(material)

    lines: list[str] = []
    lines.extend(_header(payload, brand, material))

    if payload.headline:
        lines.append(f"> {payload.headline}")
        lines.append("")

    lines.extend(_macro_block(payload))
    lines.extend(_overview_table(payload))
    lines.extend(_reviews_block(payload, src_map))
    lines.extend(_alert_block(payload, src_map))
    lines.extend(_calls_block(payload, src_map))
    lines.extend(_tension_block(payload))
    lines.extend(_sections_block(payload))
    lines.extend(_watchlist_block(payload, src_map))
    lines.extend(_appendix(payload, material))

    lines.append("---")
    lines.append(
        f"*{brand.get('institute', '')} · 本报告由 AI 管道生成, 仅供研究参考, 不构成投资建议。*"
    )
    generated = payload.generated_at or material.get("generated_at", "")
    if generated:
        lines.append(f"*生成于 {generated}*")
    return "\n".join(lines)


def render_report_file(payload: DigestPayload, body: str,
                       issue_url: str = "", date_str: str = "") -> str:
    """仓库归档版: Issue 正文前加 YAML frontmatter, 便于日后检索与 diff"""
    front = [
        "---",
        f"date: {date_str}",
        f"kind: {payload.kind}",
        f"calls: {len(payload.calls)}",
        f"fallback: {str(payload.fallback).lower()}",
    ]
    if issue_url:
        front.append(f"issue: {issue_url}")
    front.append("---")
    front.append("")
    return "\n".join(front) + body + "\n"
