"""可信度标注 —— Traffic Light 系统

🟢 high   : 一手官方来源、SEC 监管披露、同行评审论文
🟡 medium : 主流科技媒体、社区高票共识
🔴 low    : 个人博客、单源传闻、未证实的早期信号
"""

CREDIBILITY_RULES = {
    # —— 官方一手来源 ——
    "rss:openai":       "high",
    "rss:googleai":     "high",
    "rss:anthropic":    "high",
    "rss:nvidia-blog":  "high",
    "rss:msft-research":"high",
    "sec_edgar":        "high",

    # —— 学术 / IEEE ——
    "arxiv":                "high",
    "rss:ieee-spectrum-ai": "high",

    # —— 主流科技媒体（英文） ——
    "rss:theverge-ai":     "medium",
    "rss:arstechnica":     "medium",
    "rss:techcrunch-ai":   "medium",
    "rss:venturebeat-ai":  "medium",
    "rss:wired":           "medium",
    "rss:mit-tr":          "medium",
    "rss:tomshardware":    "medium",
    "rss:zdnet":           "medium",
    "rss:cnbc-tech":       "medium",
    "rss:eetimes":         "medium",
    "rss:infoq":           "medium",
    "rss:importai":        "medium",

    # —— 中文科技媒体 ——
    "rss:36kr":            "medium",
    "rss:leiphone":        "medium",

    # —— 聚合器（内容来自其他源，需交叉验证） ——
    "rss:techmeme":        "low",

    # —— 社区 / 众源 ——
    "hackernews":         "low",
    "github_trending":    "low",
    "rss:lobsters":       "low",
}

# 按前缀匹配（RSS 源可能带子 ID）
_PREFIX_RULES = {k: v for k, v in sorted(CREDIBILITY_RULES.items(), key=lambda x: -len(x[0]))}


def get_credibility(source_id: str) -> str:
    """根据信源 ID 返回默认可信度"""
    # 精确匹配
    if source_id in CREDIBILITY_RULES:
        return CREDIBILITY_RULES[source_id]
    # 前缀匹配（如 sec_edgar:8-K → sec_edgar, arxiv:cs.AI → arxiv）
    for prefix, level in _PREFIX_RULES.items():
        if source_id.startswith(prefix):
            return level
    return "low"


CREDIBILITY_EMOJI = {
    "high":   "🟢",
    "medium": "🟡",
    "low":    "🔴",
}

CREDIBILITY_LABEL = {
    "high":   "高可信",
    "medium": "中可信",
    "low":    "低可信",
}


def cred_display(level: str) -> str:
    """返回 traffic light 展示文本"""
    emoji = CREDIBILITY_EMOJI.get(level, "⚪")
    label = CREDIBILITY_LABEL.get(level, level)
    return f"{emoji} {label}"
