"""阅读清单渲染 —— Markdown 正文 + 仓库归档版(带 frontmatter)

链接一律由本层从候选记录里取, LLM 不参与 —— 见 notes.py 的同一条纪律。
"""

from radar.reading.selector import angle_label

_NOTE_LABEL = (
    ("claim", "主张"),
    ("evidence", "证据"),
    ("gap", "薄弱处"),
    ("tension", "冲突点"),
    ("followup", "延伸"),
)


def _source_line(cand: dict) -> str:
    bits = [angle_label(cand.get("angle", ""))]
    if cand.get("source"):
        bits.append(cand["source"])
    if cand.get("published_at"):
        bits.append(cand["published_at"][:10])
    if cand.get("source_traceable"):
        bits.append("可溯源一手材料")
    return " · ".join(bits)


def render_digest(picked: list[dict], skipped: list[dict], date_str: str) -> str:
    """渲染每日阅读清单正文"""
    lines = [f"# 今日值得读 · {date_str}", ""]

    if not picked:
        lines.append("*今日没有条目通过筛选。宁可空着, 不凑数。*")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"*{len(picked)} 篇入选*")
    lines.append("")

    for i, cand in enumerate(picked, 1):
        title = cand.get("title", "").strip() or "(无标题)"
        url = cand.get("url", "")
        heading = f"## {i}. [{title}]({url})" if url else f"## {i}. {title}"
        lines.append(heading)
        lines.append("")
        lines.append(f"*{_source_line(cand)}*")
        lines.append("")

        note = cand.get("note") or {}
        if note:
            for key, label in _NOTE_LABEL:
                val = (note.get(key) or "").strip()
                if val:
                    lines.append(f"**{label}** {val}")
                    lines.append("")
        else:
            # 笔记环节失败: 至少把筛选理由交代清楚, 而不是留一个空标题
            why = (cand.get("why") or "").strip()
            lines.append(f"**入选理由** {why or '未记录'}")
            lines.append("")
            lines.append("*(本篇笔记生成失败, 仅保留筛选理由)*")
            lines.append("")

        lines.append("---")
        lines.append("")

    if skipped:
        lines.append("## 未入选")
        lines.append("")
        for cand in skipped:
            title = cand.get("title", "").strip()
            if not title:
                continue
            url = cand.get("url", "")
            entry = f"- [{title}]({url})" if url else f"- {title}"
            why = (cand.get("why") or "").strip()
            if why:
                entry += f" —— {why}"
            lines.append(entry)
        lines.append("")

    return "\n".join(lines)


def render_report_file(body: str, date_str: str, picked: int,
                       issue_url: str = "") -> str:
    """仓库归档版: 正文前加 YAML frontmatter, 便于日后检索与 diff"""
    front = [
        "---",
        f"date: {date_str}",
        "kind: reading",
        f"picked: {picked}",
    ]
    if issue_url:
        front.append(f"issue: {issue_url}")
    front.append("---")
    front.append("")
    return "\n".join(front) + body + "\n"
