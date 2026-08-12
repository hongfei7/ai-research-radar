"""README 最新内参索引 —— 由 reports/ 目录重建, 而不是往顶部追加

旧实现每轮把新报头拼到 README 前面再接上"---"之后的旧内容, 于是每跑一轮
就多出一个空的 `---`(线上已积累 9 个)。这里改为标记包裹 + 整块替换, 重复
执行任意多次结果都一致。
"""

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_README = _ROOT / "README.md"
_REPORTS_DIR = _ROOT / "reports"

INDEX_START = "<!-- INDEX:START -->"
INDEX_END = "<!-- INDEX:END -->"

_MAX_ENTRIES = 10
_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_ISSUE_RE = re.compile(r"^issue:\s*(\S+)\s*$", re.MULTILINE)


def _read_issue_url(path: Path) -> str:
    """从 frontmatter 里取 Issue 链接(只读文件头, 不整篇加载)"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(400)
    except OSError:
        return ""
    m = _ISSUE_RE.search(head)
    return m.group(1) if m else ""


def build_index_lines(reports_dir: Optional[Path] = None,
                      max_entries: int = _MAX_ENTRIES) -> list[str]:
    reports_dir = reports_dir or _REPORTS_DIR
    if not reports_dir.is_dir():
        return ["_暂无归档报告_"]

    dated = []
    for p in reports_dir.iterdir():
        m = _DATE_FILE_RE.match(p.name)
        if m:
            dated.append((m.group(1), p))
    if not dated:
        return ["_暂无归档报告_"]

    dated.sort(key=lambda x: x[0], reverse=True)
    lines = []
    for date_str, path in dated[:max_entries]:
        rel = path.relative_to(_ROOT) if _ROOT in path.parents else f"reports/{path.name}"
        entry = f"- [{date_str} 内参]({rel})"
        issue_url = _read_issue_url(path)
        if issue_url:
            entry += f" · [Issue]({issue_url})"
        lines.append(entry)
    return lines


def _replace_block(text: str, block: str) -> str:
    """把标记之间的内容换成 block; 标记不存在时就地建立标记区"""
    start = text.find(INDEX_START)
    end = text.find(INDEX_END)
    if start != -1 and end != -1 and end > start:
        return text[:start] + block + text[end + len(INDEX_END):]

    # 首次运行(或 README 被手工改过): 替换 "## 最新内参" 整节, 顺带清掉旧的空分隔符
    section = re.search(r"^## 最新内参\s*$", text, re.MULTILINE)
    if section:
        rest = text[section.end():]
        next_heading = re.search(r"^## ", rest, re.MULTILINE)
        tail = rest[next_heading.start():] if next_heading else ""
        return text[:section.start()] + block + "\n\n" + tail

    return text.rstrip() + "\n\n" + block + "\n"


def update_readme_index(issue_url: str = "", reports_dir: Optional[Path] = None,
                        readme_path: Optional[Path] = None) -> bool:
    """重建 README 的最新内参索引区; 内容无变化时不写盘"""
    readme_path = readme_path or _README
    if not readme_path.exists():
        logger.warning(f"README not found: {readme_path}")
        return False

    lines = build_index_lines(reports_dir)
    block = "\n".join([INDEX_START, "", "## 最新内参", "", *lines, "", INDEX_END])

    original = readme_path.read_text(encoding="utf-8")
    updated = _replace_block(original, block)
    if updated == original:
        return False
    readme_path.write_text(updated, encoding="utf-8")
    logger.info(f"README index updated: {len(lines)} entries")
    return True
