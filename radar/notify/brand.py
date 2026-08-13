"""品牌与共用展示常量 —— 三个渲染器过去各自复制了一份默认值"""

DEFAULT_BRAND = {
    "institute": "Sterling 证券研究",
    "analyst": "Ayer",
    "analyst_title": "TMT 首席分析师",
}

# 判断序号的中文写法
CALL_ORDINALS = ("一", "二", "三", "四", "五", "六", "七", "八")

# 证据行的可信度标签
CRED_LABEL = {"high": "高可信", "medium": "中可信", "low": "低可信"}


def brand_of(cfg: dict) -> dict:
    """从 config 取品牌配置, 缺项用默认值补齐"""
    configured = (cfg or {}).get("notify", {}).get("brand", {}) or {}
    return {**DEFAULT_BRAND, **configured}


def ordinal(n: int) -> str:
    """1 → "一"; 超出预置范围时退回阿拉伯数字"""
    if 1 <= n <= len(CALL_ORDINALS):
        return CALL_ORDINALS[n - 1]
    return str(n)


def source_label(source: dict) -> str:
    """证据行尾部的信源属性标签: 一手 / 二手 / 低可信"""
    cred = (source.get("credibility") or "").strip().lower()
    if "low" in cred:
        return "低可信"
    return "一手" if source.get("is_primary_source") else "二手"


def caveat_label(source: dict) -> str:
    """只在有信息量时出标签: 低可信是警示, 一手是加分, 二手是默认值不必占位"""
    label = source_label(source)
    return label if label in ("低可信", "一手") else ""


# 采集器 id → 出版方。source 字段存的是采集器标识("web_search"、"rss:36kr"),
# 那是系统实现细节, 对读者零价值 —— 报给读者的必须是"谁发的"。
_SOURCE_PUBLISHER = {
    "rss:36kr": "36氪", "rss:leiphone": "雷锋网", "rss:qbitai": "量子位",
    "rss:geekpark": "极客公园", "rss:tmtpost": "钛媒体", "rss:ifanr": "爱范儿",
    "rss:theverge-ai": "The Verge", "rss:techcrunch-ai": "TechCrunch",
    "rss:arstechnica": "Ars Technica", "rss:arstechnica-ai": "Ars Technica",
    "rss:the-decoder": "The Decoder", "rss:wired": "Wired",
    "rss:mit-tr": "MIT Tech Review", "rss:venturebeat-ai": "VentureBeat",
    "rss:tomshardware": "Tom's Hardware", "rss:zdnet": "ZDNet",
    "rss:eetimes": "EE Times", "rss:infoq": "InfoQ", "rss:techmeme": "Techmeme",
    "rss:ieee-spectrum-ai": "IEEE Spectrum", "rss:importai": "Import AI",
    "rss:semiengineering": "Semiconductor Engineering",
    "rss:stratechery": "Stratechery", "rss:interconnects": "Interconnects",
    "rss:simonwillison": "Simon Willison", "rss:lobsters": "Lobsters",
    "rss:openai": "OpenAI", "rss:googleai": "Google AI", "rss:deepmind": "DeepMind",
    "rss:nvidia-blog": "NVIDIA", "rss:msft-research": "Microsoft Research",
    "rss:aws-ml": "AWS", "rss:hf-blog": "Hugging Face",
    "huggingface:papers": "HF Daily Papers", "hackernews": "Hacker News",
    "github_trending": "GitHub Trending", "arxiv": "arXiv", "sec_edgar": "SEC",
}

# 搜索类采集器没有出版方字段, 从 URL 域名反查
_DOMAIN_PUBLISHER = {
    "news.qq.com": "腾讯新闻", "toutiao.com": "今日头条", "zhihu.com": "知乎",
    "zhuanlan.zhihu.com": "知乎", "xueqiu.com": "雪球", "sohu.com": "搜狐",
    "36kr.com": "36氪", "thepaper.cn": "澎湃", "sina.cn": "新浪",
    "k.sina.cn": "新浪", "ithome.com": "IT之家", "leiphone.com": "雷锋网",
    "technews.tw": "科技新报", "cnbeta.com.tw": "cnBeta",
    "techmeme.com": "Techmeme", "tomshardware.com": "Tom's Hardware",
    "techcrunch.com": "TechCrunch", "theverge.com": "The Verge",
    "reddit.com": "Reddit", "github.com": "GitHub", "zdnet.com": "ZDNet",
    "eetimes.com": "EE Times", "infoq.com": "InfoQ", "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg", "nvidia.com": "NVIDIA", "openai.com": "OpenAI",
    "blogs.nvidia.com": "NVIDIA", "arm.com": "Arm", "intc.com": "Intel",
}


def publisher_name(source: dict) -> str:
    """出版方名称; 未收录的域名退化为域名本身, 总好过露出 "web_search" """
    sid = (source.get("source") or "").strip()
    if sid in _SOURCE_PUBLISHER:
        return _SOURCE_PUBLISHER[sid]
    for prefix, name in _SOURCE_PUBLISHER.items():
        if sid.startswith(prefix):      # arxiv:cs.AI / sec_edgar:8-K
            return name

    from urllib.parse import urlparse
    try:
        host = urlparse(source.get("url") or "").netloc.lower()
    except ValueError:
        host = ""
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return ""
    if host in _DOMAIN_PUBLISHER:
        return _DOMAIN_PUBLISHER[host]
    # 去掉子域再试一次(mp.weixin.qq.com → weixin.qq.com → qq.com)
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in _DOMAIN_PUBLISHER:
            return _DOMAIN_PUBLISHER[candidate]
    return host


def short_date(iso_str: str) -> str:
    """ISO 时间 → MM-DD, 解析失败返回空串"""
    from radar.models import parse_iso
    dt = parse_iso(iso_str)
    return dt.strftime("%m-%d") if dt else ""


# 方向箭头。取自 extract 阶段产出、已按 coverage 白名单校验的 event.direction,
# 不让撰稿 LLM 重新判一次 —— 这样箭头的可靠度与标的本身一致。
DIRECTION_ARROW = {"positive": "↑", "negative": "↓", "neutral": "→"}

# 抬头最多两个标的: 三个以上就会折行, 而快报本来就是"一眼扫完"的产品
_MAX_HEADER_TICKERS = 2


def ticker_line(tickers: list, direction: dict | None = None,
                max_tickers: int = _MAX_HEADER_TICKERS) -> str:
    """标的串: "长电科技 ↑｜英伟达 ↓"

    只放有多空判断的标的。一个没有方向的标的挤在只能容两个位置的抬头里,
    既不帮读者决策, 还会让同一行出现"有的带箭头有的光秃秃"的错乱观感。
    一个方向都没有时退回裸标的, 保证抬头不空。
    """
    direction = direction or {}

    def _arrow(tk: str) -> str:
        return DIRECTION_ARROW.get(str(direction.get(tk, "")).strip().lower(), "")

    tickers = tickers or []
    directional = [tk for tk in tickers if _arrow(tk) in ("↑", "↓")]
    if directional:
        parts = [f"{tk} {_arrow(tk)}" for tk in directional[:max_tickers]]
    else:
        parts = list(tickers[:max_tickers])
    # 全角竖线分隔标的, 与后面的 " · 时间" 拉开层级
    return "｜".join(parts)


def alert_header(material: dict, brand: dict, product: str, when: str) -> list[str]:
    """快报抬头, 两行:

        Sterling 证券研究 · Ayer 首席快报
        英伟达 ↑ · 台积电 ↓ · 08月13日 15:42

    第一行落品牌与署名 —— 收件人要一眼知道这是谁发的。第二行才是标的与时间;
    一轮最多推 3 条快报, 不带标的就完全无法区分是哪只票的什么事。
    两者挤一行会长到折行, 反而更难读。
    """
    brand = brand or DEFAULT_BRAND
    ev = (material or {}).get("event") or {}
    title = " · ".join(b for b in [
        brand.get("institute", ""),
        " ".join(b for b in [brand.get("analyst", ""), product] if b),
    ] if b)
    subtitle = " · ".join(b for b in [
        ticker_line(ev.get("tickers"), ev.get("direction")), when,
    ] if b)
    return [title, subtitle] if subtitle else [title]
