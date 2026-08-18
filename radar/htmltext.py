"""HTML → 正文纯文本

被两个互不相关的子系统共用: 阅读流抓文章正文(radar/reading/fulltext.py)、
SEC 采集器抓申报正文(radar/collectors/sec_edgar.py)。放在这里而不是任一子系统内,
是为了避免采集层反过来依赖阅读层 —— 那种依赖方向会让人读不懂分层。
"""

import re

from selectolax.parser import HTMLParser

_MAX_CHARS = 6000

# 正文容器候选, 命中即止。顺序即优先级: 语义标签优于类名猜测
_CONTENT_SELECTORS = (
    "article", "main", "[role=main]",
    ".post-content", ".entry-content", ".article-content",
    ".post-body", ".content",
)

# 抓下来一定要剥掉的噪声节点, 否则导航与推荐位会混进正文
_NOISE_SELECTORS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript")

_TAG_LINE_RE = re.compile(r"\s*<\s*/?\s*[a-zA-Z][^>]*>\s*")


def extract_text(html: str, max_chars: int = _MAX_CHARS) -> str:
    """从 HTML 中抽取正文纯文本"""
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
    except Exception:
        return ""

    for sel in _NOISE_SELECTORS:
        for node in tree.css(sel):
            node.decompose()

    node = None
    for sel in _CONTENT_SELECTORS:
        node = tree.css_first(sel)
        if node is not None:
            break
    if node is None:
        node = tree.body
    if node is None:
        return ""

    text = node.text(separator="\n", strip=True)
    # 有些站点(实测量子位)把 <img> 之类的标签转义后当正文文本吐出来, 解析器不会碰它,
    # 结果一段"< img id=... >"混进素材。这里按行剔掉纯标签行。
    text = "\n".join(
        line for line in text.split("\n") if not _TAG_LINE_RE.fullmatch(line)
    )
    # 压掉连续空行, 但保留段落边界 —— 段落结构是判断论证有没有跳步的线索
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]
