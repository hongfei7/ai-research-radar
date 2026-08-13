"""标题与文本规范化 —— 采集层脏数据的统一清洗出口

抓取标题普遍带三类噪音, 直接进事件线会污染报告版式:
- 站点后缀: "… - 财新网" / "…_新浪科技_新浪网" / "… | Tom's Hardware"
- 话题标签尾巴: "… #长电科技 #半导体封测 #Chiplet"
- 自我重复: 搜索类采集器偶发把同一标题拼两遍
另有全小写术语("谷歌第八代tpu详解"), 中英混排下无法靠 \b 定位, 按 ASCII 连续段处理。
"""

import re

# —— 站点后缀识别 ——
# 分隔符后 2-20 字的短尾巴, 命中站点特征才剥离(避免误删 "… - 深度分析" 这类正文)
# 连字符放在字符组末尾, 否则会被解析成范围
_SEP_CHARS = "–—_|｜-"
_SUFFIX_RE = re.compile(rf"\s*[{_SEP_CHARS}]\s*([^{_SEP_CHARS}]{{2,20}})\s*$")

# 中文站点特征字(出现即判定为站点名)
_CN_SITE_MARKERS = (
    "网", "科技", "财经", "新闻", "日报", "晚报", "时报", "周刊",
    "资讯", "财讯", "见闻", "早报", "快报", "在线", "社区",
    "之家", "头条", "研究院", "观察",
)
# 中文站点全名(不含上述特征字的)
_CN_SITE_NAMES = (
    "36氪", "虎嗅", "钛媒体", "雷锋", "机器之心", "量子位", "澎湃",
    "界面", "第一财经", "华尔街见闻", "格隆汇", "同花顺", "东方财富",
    "今日头条", "百家号", "知乎", "微博", "公众号", "财新", "路透",
)
# 英文媒体名(小写比较)
_EN_SITE_NAMES = {
    "techcrunch", "the verge", "theverge", "tom's hardware", "toms hardware",
    "reuters", "bloomberg", "cnbc", "zdnet", "venturebeat", "ars technica",
    "wired", "engadget", "ieee spectrum", "nikkei asia", "scmp", "digitimes",
    "the information", "the register", "techmeme", "business insider",
    "financial times", "wall street journal", "wsj", "cnet", "gizmodo",
    "mit technology review", "seeking alpha", "barron's", "barrons",
}

# —— 术语大小写 ——
_TERM_CASE = {
    "ai": "AI", "agi": "AGI", "gpu": "GPU", "cpu": "CPU", "tpu": "TPU",
    "npu": "NPU", "dpu": "DPU", "hbm": "HBM", "llm": "LLM", "llms": "LLMs",
    "nand": "NAND", "dram": "DRAM", "ssd": "SSD", "soc": "SoC", "api": "API",
    "asic": "ASIC", "fpga": "FPGA", "hpc": "HPC", "cowos": "CoWoS",
    "sram": "SRAM", "pcie": "PCIe", "nvlink": "NVLink", "cuda": "CUDA",
    "rag": "RAG", "moe": "MoE", "sdk": "SDK", "ipo": "IPO", "capex": "capex",
    "eda": "EDA", "euv": "EUV", "duv": "DUV", "iot": "IoT", "5g": "5G",
}

# 话题标签尾巴: 标签内可含空格("#AI 算力"), 但必须整体位于末尾且前有空白
# (前置 \s+ 要求可避免误伤 "C# 教程" 这类标题)
_HASHTAG_TAIL_RE = re.compile(r"\s+(?:#[^#]{1,20})+$")

# SEO 关键词尾巴 / 栏目路径: 末尾连着 ≥2 段竖线分隔的短词
#   "…交付逾30万枚|谷歌|英伟达|纳德拉|知名企业"
#   "…台積助攻 | 科技產業 | 產經 | 聯合新聞網"
# 要求 ≥2 段, 单段 "标题 | 站点名" 交给站点后缀逻辑处理, 不在这里误伤
_KEYWORD_TAIL_RE = re.compile(r"(?:\s*[|｜]\s*[^|｜]{1,16}){2,}\s*$")
_SELF_REPEAT_RE = re.compile(r"^(.{8,}?)[\s　]*\1$", re.DOTALL)
_WS_RE = re.compile(r"[\s　]+")
_ASCII_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9']*")

# 清洗后短于此长度视为清洗过度, 回退原标题。
# 中文标题信息密度高, "台积电扩产"才 5 个字, 门槛设 6 会让这类短标题的
# 站点后缀剥不掉(实测 "测试标题-快科技" 因剩 4 字被整体回退)。
_MIN_KEEP_LEN = 4


def _is_site_suffix(text: str) -> bool:
    """判断分隔符后的短尾巴是否为站点名"""
    t = text.strip()
    if not t:
        return False
    low = t.lower()
    if low in _EN_SITE_NAMES:
        return True
    if any(name in t for name in _CN_SITE_NAMES):
        return True
    # 中文特征字: 仅当尾巴以之结尾或整体较短时判定, 避免误伤 "AI 芯片新闻发布会" 这类正文
    if len(t) <= 12 and any(marker in t for marker in _CN_SITE_MARKERS):
        return True
    return False


def _strip_site_suffix(title: str, max_rounds: int = 3) -> str:
    """反复剥离末尾站点名(如 "…_新浪科技_新浪网" 需要剥两轮)"""
    for _ in range(max_rounds):
        m = _SUFFIX_RE.search(title)
        if not m or not _is_site_suffix(m.group(1)):
            break
        # 连带清掉残留的分隔符("…-快科技--科技改变未来" 剥一轮后会留下尾部横线)
        stripped = title[: m.start()].strip(" 　" + _SEP_CHARS)
        if len(stripped) < _MIN_KEEP_LEN:
            break
        title = stripped
    return title


def _fix_term_case(title: str) -> str:
    """全小写术语还原为业界写法; 只动整段小写的 ASCII 串, 不碰已有大小写的词"""
    def _repl(m: re.Match) -> str:
        word = m.group(0)
        if not word.islower():
            return word
        return _TERM_CASE.get(word, word)

    return _ASCII_RUN_RE.sub(_repl, title)


def clean_title(title: str) -> str:
    """清洗抓取标题, 无法安全清洗时返回原标题(去空白后)"""
    if not title:
        return ""
    original = _WS_RE.sub(" ", title).strip()
    if not original:
        return ""

    cleaned = original

    # 1. 自我重复("XXX XXX" → "XXX")
    m = _SELF_REPEAT_RE.match(cleaned)
    if m:
        cleaned = m.group(1).strip()

    # 2. 话题标签尾巴
    cleaned = _HASHTAG_TAIL_RE.sub("", cleaned).strip()

    # 2.5 SEO 关键词串 / 栏目路径尾巴
    stripped = _KEYWORD_TAIL_RE.sub("", cleaned).strip()
    if len(stripped) >= _MIN_KEEP_LEN:
        cleaned = stripped

    # 3. 站点后缀
    cleaned = _strip_site_suffix(cleaned)

    # 4. 术语大小写
    cleaned = _fix_term_case(cleaned)

    cleaned = cleaned.strip(" 　-–—_|｜·,，、")
    if len(cleaned) < _MIN_KEEP_LEN:
        return original
    return cleaned


# 标签页/索引页/检索结果页: 没有具体内容, 进了事件线只会变成一条无法引用的证据
# (实测搜索返回过 ithome.com/tags/台积电 这种聚合页, 还被当成 sig 8 的事件)
_INDEX_PATH_RE = re.compile(
    r"/(?:tags?|topics?|category|categories|channel|column|search|list|archives?)"
    r"(?:/|$|\?)",
    re.IGNORECASE,
)


def is_index_page(url: str) -> bool:
    """判断 URL 是否为标签页/栏目页等聚合页面(无具体内容)"""
    if not url:
        return True
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not parsed.netloc:
        return True
    path = parsed.path or "/"
    if path in ("", "/"):
        return True          # 站点首页
    return bool(_INDEX_PATH_RE.search(path))


# 聚合帖(晚报/早报/汇总)是"一篇 N 个事件"的容器, 对这套系统是纯负担:
# 词汇分布接近语料均值, 和任何事件的 Jaccard 都偏高(实测全场最高的假阳性
# 0.208 就来自氪星晚报); 标的动辄十几个, 是标的磁铁; 作为证据链接会把读者
# 带到一个找不到对应内容的大杂烩页面, 直接违背"每条判断可溯源"的承诺。
_DEFAULT_DIGEST_PATTERNS = (
    "晚报", "早报", "午报", "周报", "速览", "汇总", "盘点",
    "一周回顾", "一周速览", "本周要闻", "今日热点", "要闻回顾",
    "每日精选", "24小时", "新闻早餐", "资讯合集",
)


# 多话题结构判据: 标题被分隔符切成 N 段且每段都有实质内容。
# 穷举各家栏目名追不完(线上就漏了 36氪的 "8点1氪"), 结构特征才是稳的。
_TOPIC_SEP_RE = re.compile(r"[；;丨|｜]")
_MIN_TOPIC_SEGMENTS = 3
_MIN_SEGMENT_LEN = 6


def _looks_multi_topic(title: str) -> bool:
    segments = [s.strip() for s in _TOPIC_SEP_RE.split(title)]
    substantial = [s for s in segments if len(s) >= _MIN_SEGMENT_LEN]
    return len(substantial) >= _MIN_TOPIC_SEGMENTS


def is_digest_title(title: str, patterns: tuple | list | None = None,
                    source: str = "", exempt_sources: tuple | list | None = None) -> bool:
    """判断标题是否为多话题聚合帖

    两条判据取或: 命中栏目名, 或结构上就是多话题拼盘。
    在站点后缀已被 clean_title 剥掉之后判断, 避免"经济日报"这类站点名误伤。

    exempt_sources 里的信源只走关键词判据 —— Import AI 这类高质量周报也用
    分号列话题, 结构上确实是周报, 但内容价值远高于氪星晚报, 不该一并丢掉。
    """
    if not title:
        return False
    pats = patterns if patterns is not None else _DEFAULT_DIGEST_PATTERNS
    if any(p and p in title for p in pats):
        return True
    if source and any(e and source.startswith(e) for e in (exempt_sources or ())):
        return False
    return _looks_multi_topic(title)


def clip_sentence(text: str, max_len: int, hard: bool = True) -> str:
    """按句子边界裁剪, 避免半句截断

    Args:
        hard: 找不到句子边界时是否硬截断加省略号。
              False 表示宁可超长也要保留完整句子 —— 对判断类文本,
              一个被砍断的结论("…谁就能留住…")比一个略长的完整结论更糟。
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    window = text[:max_len]
    for punct in ("。", "！", "？", ".", "；", ";"):
        idx = window.rfind(punct)
        if idx >= max_len // 2:
            return window[: idx + 1]
    if not hard:
        # 退而求其次: 截到第一个句末标点, 保住完整的第一句
        for punct in ("。", "！", "？"):
            idx = text.find(punct)
            if idx != -1:
                return text[: idx + 1]
        return text
    return window.rstrip() + "…"


def strip_markdown(text: str) -> str:
    """去掉 LLM 自由文本里的 markdown 记号, 供表格/列表内联渲染使用"""
    if not text:
        return ""
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = _WS_RE.sub(" ", text)
    return text.strip()
