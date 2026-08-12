"""晨报 Issue 正文渲染 —— DigestPayload → GitHub Issue markdown(完整版)

GitHub Issue 支持完整 markdown, 用标准标题/列表/引用语法。
"""

from radar.notify.types import DigestPayload


def _dir_label(d: str) -> str:
    return {"positive": "看多", "negative": "看空", "neutral": "中性", "mixed": "分歧"}.get(d, "")


def render_issue_body(payload: DigestPayload, site_url: str = "") -> str:
    lines: list[str] = []
    if payload.headline:
        lines.append(f"> {payload.headline}")
        lines.append("")

    for section in payload.sections:
        if section.is_empty():
            continue
        lines.append(f"## {section.heading}")
        lines.append("")
        for para in section.paragraphs:
            if para.strip():
                lines.append(para.strip())
                lines.append("")
        for item in section.items:
            tickers = f" `[{', '.join(item.tickers)}]`" if item.tickers else ""
            direction = _dir_label(item.direction)
            dir_str = f" · {direction}" if direction else ""
            lines.append(f"### {item.title}{tickers}")
            lines.append(f"重要性 {item.significance}/10{dir_str}")
            lines.append("")
            if item.summary:
                lines.append(item.summary)
                lines.append("")
            if item.why:
                lines.append(f"**影响**: {item.why}")
                lines.append("")
            if item.watch:
                lines.append(f"**关注**: {item.watch}")
                lines.append("")

    lines.append("---")
    lines.append("*本晨报由 AI 投研雷达自动生成, 仅作研究素材, 不构成投资建议。*")
    if site_url:
        lines.append(f"*实时看板: {site_url} · 生成于 {payload.generated_at}*")
    return "\n".join(lines)
