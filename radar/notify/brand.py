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
