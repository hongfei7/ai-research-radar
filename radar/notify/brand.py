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


def short_date(iso_str: str) -> str:
    """ISO 时间 → MM-DD, 解析失败返回空串"""
    from radar.models import parse_iso
    dt = parse_iso(iso_str)
    return dt.strftime("%m-%d") if dt else ""


# 方向箭头。取自 extract 阶段产出、已按 coverage 白名单校验的 event.direction,
# 不让撰稿 LLM 重新判一次 —— 这样箭头的可靠度与标的本身一致。
DIRECTION_ARROW = {"positive": "↑", "negative": "↓", "neutral": "→"}

_MAX_HEADER_TICKERS = 3


def ticker_line(tickers: list, direction: dict | None = None,
                max_tickers: int = _MAX_HEADER_TICKERS) -> str:
    """标的串: "长电科技 ↑ · 英伟达 ↓ · 台积电"

    只给多空标箭头 —— 中性画一排 "→" 是纯噪音, 占位置又不带信息。
    有明确方向的标的排在前面: 抬头位置有限, 先给读者最能决策的那几个。
    """
    direction = direction or {}

    def _arrow(tk: str) -> str:
        return DIRECTION_ARROW.get(str(direction.get(tk, "")).strip().lower(), "")

    def _directional(tk: str) -> bool:
        return _arrow(tk) in ("↑", "↓")

    # sorted 稳定, 组内保留原有的频次降序
    ranked = sorted(tickers or [], key=lambda tk: 0 if _directional(tk) else 1)
    parts = []
    for tk in ranked[:max_tickers]:
        parts.append(f"{tk} {_arrow(tk)}".strip() if _directional(tk) else tk)
    return " · ".join(parts)


def alert_header(material: dict, product: str, when: str) -> str:
    """快报抬头: "英伟达 ↑ · 台积电 ↑｜首席快报 · 15:42"

    一轮最多推 3 条快报, 抬头若不带标的就完全无法区分是哪只票的什么事。
    """
    ev = (material or {}).get("event") or {}
    line = ticker_line(ev.get("tickers"), ev.get("direction"))
    left = f"{line}｜{product}" if line else product
    return f"{left} · {when}" if when else left
