"""内参 Issue 正文渲染 —— DigestPayload → GitHub Issue markdown(完整版)

GitHub Issue 支持完整 markdown。正文与推送同稿; 末尾附数据附录 ——
评分/标的/信源等元数据全部收在这里, 不进推送(零元数据原则)。
"""

from radar.notify.types import DigestPayload, KIND_BREAKING

_DEFAULT_BRAND = {
    "institute": "观澜研究院",
    "analyst": "沈砚舟",
    "analyst_title": "TMT 首席分析师",
}

_DIR_LABEL = {"positive": "看多", "negative": "看空", "neutral": "中性", "mixed": "分歧"}


def render_issue_body(payload: DigestPayload, site_url: str = "",
                      material: dict | None = None,
                      brand: dict | None = None) -> str:
    brand = brand or _DEFAULT_BRAND
    lines: list[str] = []

    # 报头
    product = payload.title.split("|", 1)[0].strip() if payload.title else "内参"
    lines.append(f"# {brand.get('institute', '')} | {product}")
    byline = " · ".join(b for b in [
        payload.title.split("|", 1)[1].strip() if "|" in payload.title else "",
        f"{brand.get('analyst', '')}({brand.get('analyst_title', '')})",
    ] if b)
    if byline:
        lines.append(f"*{byline}*")
    lines.append("")

    if payload.headline:
        lines.append(f"> {payload.headline}")
        lines.append("")

    # 正文
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

    # 数据附录: 元数据的家
    events = (material or {}).get("events") or []
    if events:
        lines.append("---")
        lines.append("")
        lines.append("## 附录: 事件线与数据")
        lines.append("")
        lines.append("| 事件 | 标的 | 重要性 | 状态 | 信源 |")
        lines.append("|---|---|---|---|---|")
        for ev in events[:20]:
            tickers = ", ".join((ev.get("tickers") or [])[:4])
            status = "活跃" if ev.get("is_active") else "已了结"
            lines.append(
                f"| {ev.get('title', '')} | {tickers} | "
                f"{ev.get('significance', 0)}/10 | {status} | {ev.get('source_count', 0)} |"
            )
        lines.append("")

    risk = (material or {}).get("risk_material") or []
    if risk:
        lines.append("## 附录: 风险信号")
        lines.append("")
        for r in risk[:5]:
            opinion = r.get("second_opinion") or ""
            cred = r.get("credibility") or ""
            lines.append(f"- **{r.get('title', '')}**({cred}){opinion}")
        lines.append("")

    lines.append("---")
    lines.append(f"*{brand.get('institute', '')} · 本报告由 AI 管道生成, 仅供研究参考, 不构成投资建议。*")
    if site_url:
        lines.append(f"*实时看板: {site_url} · 生成于 {payload.generated_at}*")
    return "\n".join(lines)
