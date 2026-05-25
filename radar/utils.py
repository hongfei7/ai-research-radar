"""通用工具函数"""

import re

_DEFAULT_TRUNCATE_LEN = 800


def truncate(text: str, max_len: int = _DEFAULT_TRUNCATE_LEN) -> str:
    """去除 HTML 标签、压缩空白后截断到 max_len 字符。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = " ".join(text.split())
    return text[:max_len]
