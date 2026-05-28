"""数据模型定义：Item, Event, Situation"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Item:
    """流水线上的条目，采集阶段填上半部分，处理阶段补全下半部分"""

    # —— 采集阶段 ——
    id: str                              # URL 规范化后的 SHA1
    title: str
    url: str
    source: str                          # 信源标识，如 "arxiv" / "rss:openai-blog"
    source_type: str                     # "tech" | "market"
    published_at: str                    # ISO8601
    fetched_at: str                      # ISO8601
    raw_summary: str                     # 原始摘要/正文片段，≤1500字符

    # —— 处理阶段（MiniMax 筛选 + 提取） ——
    relevance_score: int = 0             # 0-10
    relevance_reason: str = ""
    tickers: list = field(default_factory=list)   # 映射到的覆盖标的 name
    themes: list = field(default_factory=list)    # 映射到的投资主线 key
    direction: dict = field(default_factory=dict) # {标的name: "positive|negative|neutral"}
    cn_summary: str = ""                 # 中文三要素摘要
    so_what: str = ""                    # 对分析师意味着什么
    processed_at: str = ""               # ISO8601

    # —— 可信度 ——
    credibility: str = ""                # 🟢 high | 🟡 medium | 🔴 low（采集阶段按信源初标，LLM可修正）

    # —— 溯源 ——
    is_primary_source: bool = True       # 是否一手报道（false = 转载/聚合）
    original_source_url: str = ""        # 如为转载，标注原始出处 URL

    # —— 视觉分析 ——
    image_url: str = ""                  # 文章配图/缩略图 URL
    visual_analysis: str = ""            # MiniMax 图片理解结果

    # —— 反向观点 ——
    second_opinion: str = ""             # 对已有分析的替代解读或遗漏点

    # —— 聚类阶段 ——
    event_id: Optional[str] = None       # 所属事件 cluster ID
    is_new_event: bool = False           # 本条目是否触发了新事件
    is_event_update: bool = False        # 本条目是否更新了已有事件

    def to_dict(self) -> dict:
        d = asdict(self)
        # 确保 direction 等字段可序列化
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        # 处理可能缺失的处理阶段字段
        defaults = {
            "relevance_score": 0,
            "relevance_reason": "",
            "tickers": [],
            "themes": [],
            "direction": {},
            "cn_summary": "",
            "so_what": "",
            "processed_at": "",
            "credibility": "",
            "is_primary_source": True,
            "original_source_url": "",
            "image_url": "",
            "visual_analysis": "",
            "second_opinion": "",
            "event_id": None,
            "is_new_event": False,
            "is_event_update": False,
        }
        for k, v in defaults.items():
            d.setdefault(k, v)
        return cls(**{k: d[k] for k in [
            "id", "title", "url", "source", "source_type",
            "published_at", "fetched_at", "raw_summary",
            "relevance_score", "relevance_reason", "tickers", "themes",
            "direction", "cn_summary", "so_what", "processed_at",
            "credibility", "is_primary_source", "original_source_url",
            "image_url", "visual_analysis", "second_opinion",
            "event_id", "is_new_event", "is_event_update",
        ]})


@dataclass
class Event:
    """事件聚类 —— 将讲同一件事的多条来源合并成的"事件线" """

    event_id: str
    title: str                           # LLM 生成的事件标题
    summary: str                         # LLM 生成的事件摘要，≤150字
    tickers: list = field(default_factory=list)
    themes: list = field(default_factory=list)
    direction: dict = field(default_factory=dict)
    item_ids: list = field(default_factory=list)     # 属于此事件的所有条目 id
    source_count: int = 0                # 来源数
    first_seen_at: str = ""              # ISO8601
    last_updated_at: str = ""            # ISO8601
    is_active: bool = True               # 是否仍在活跃
    significance: int = 0                # 重要性 0-10
    status: str = "developing"           # "developing" | "stable" | "resolved"
    deep_analysis: str = ""              # 事件深度分析（多空逻辑/驱动因素/市场影响）
    # 代表向量(用于相似度比较)，不序列化到 JSON
    embedding: Optional[list] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("embedding", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d.pop("embedding", None)
        defaults = {
            "tickers": [],
            "themes": [],
            "direction": {},
            "item_ids": [],
            "source_count": 0,
            "first_seen_at": "",
            "last_updated_at": "",
            "is_active": True,
            "significance": 0,
            "status": "developing",
            "deep_analysis": "",
        }
        for k, v in defaults.items():
            d.setdefault(k, v)
        return cls(**d)


@dataclass
class Situation:
    """当前态势 —— 由 LLM 持续重写的滚动综述"""

    generated_at: str = ""               # ISO8601
    text: str = ""                       # ≤220字中文态势综述
    since: str = ""                      # 覆盖时间范围起点
    active_event_count: int = 0
    key_themes: list = field(default_factory=list)
    last_telegram_digest_at: str = ""    # ISO8601，上次 Telegram 兜底推送时间
    morning_brief_date: str = ""         # 上次晨报推送日期，用于 Telegram 每日去重
    last_wecom_digest_at: str = ""      # ISO8601，上次企业微信兜底推送时间
    cross_analysis: str = ""             # 交叉综合分析文本
    trend_spotting: str = ""             # 趋势发现文本

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Situation":
        defaults = {
            "generated_at": "",
            "text": "",
            "since": "",
            "active_event_count": 0,
            "key_themes": [],
            "last_telegram_digest_at": "",
            "morning_brief_date": "",
            "last_wecom_digest_at": "",
            "cross_analysis": "",
            "trend_spotting": "",
        }
        for k, v in defaults.items():
            d.setdefault(k, v)
        return cls(**d)


def utcnow_iso() -> str:
    """返回当前 UTC 时间 ISO8601 字符串"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    """宽松的 ISO8601/RFC2822 解析，始终返回带时区的 datetime"""
    if not s:
        return None
    s_clean = s.strip().replace("Z", "+00:00")
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%a, %d %b %Y %H:%M:%S %z",
    ]:
        try:
            return datetime.strptime(s_clean, fmt)
        except ValueError:
            continue
    for fmt in [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%a, %d %b %Y %H:%M:%S %Z",
    ]:
        try:
            dt = datetime.strptime(s_clean, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.strptime(s_clean[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def today_str(tz: str = "Asia/Hong_Kong") -> str:
    """返回当前日期字符串 YYYY-MM-DD，按指定时区"""
    try:
        from zoneinfo import ZoneInfo
        tz_obj = ZoneInfo(tz)
    except Exception:
        tz_obj = timezone.utc
    return datetime.now(tz_obj).strftime("%Y-%m-%d")


def get_effective_date(item) -> datetime | None:
    """只根据 published_at 判断有效日期，失败返回 None（不 fallback 到 fetched_at/processed_at）"""
    return parse_iso(item.published_at)


def get_event_effective_date(event) -> datetime | None:
    """返回事件的有效日期，优先 last_updated_at，fallback first_seen_at"""
    return parse_iso(event.last_updated_at) or parse_iso(event.first_seen_at)


def compute_effective_score(item, half_life_hours: float = 4) -> float:
    """时间衰减后的有效分数: score / (1 + hours_old / half_life)

    日期未知的条目不衰减（hours_old = 0），避免搜索类源被过度惩罚。
    """
    try:
        score = float(item.relevance_score or 0)
    except (TypeError, ValueError):
        score = 0.0
    dt = get_effective_date(item)
    if dt is None:
        return score
    hours_old = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    if hours_old < 0:
        hours_old = 0
    return score / (1.0 + hours_old / half_life_hours)


def format_relative_time(iso_str: str) -> str:
    """将 ISO8601 时间戳转为相对时间描述（"X分钟前" / "X小时前" / "X天前"）"""
    dt = parse_iso(iso_str)
    if dt is None:
        return "时间未知"
    now_dt = datetime.now(timezone.utc)
    diff = now_dt - dt
    if diff.total_seconds() < 0:
        return "刚刚"
    minutes = int(diff.total_seconds() / 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时前"
    days = hours // 24
    if days < 30:
        return f"{days}天前"
    months = days // 30
    return f"{months}个月前"
