"""覆盖度体检 —— 把"盲区"从 LLM 的印象变成可计量的指标

为什么需要它: 内参每期由 LLM 写一段 blindspot, 但 LLM 只看得到 24h 素材窗,
分不清"今天恰好没有"与"我们这条通道从来就拿不到"。实测过一个例子: 某期盲区写
"完全未涉及 AMD 的实质信息", 而 AMD 在 83 天归档里被标注了 1695 次 —— 它天天出现,
只是从来不出现在一手披露里。这种区别只有计量能给出。

台积电的一手通道断了至少 83 天没人发现, 正是因为没有任何东西在测量它。

核心指标是**自有披露**, 不是笼统的"一手来源"。这个区别是必须的: 一条英伟达官方博客
只要提到台积电, 在"一手来源"口径下就算台积电有一手覆盖 —— 而这正是盲区抱怨的那件事
("只能从英伟达自披露口径间接外推")。用自有披露口径, 台积电当时会立刻显形。

判档必须先问"配置上本来就该有这条通道吗", 否则告警会在所有标的上一起响 —— 实测
不加这层区分时 37 个标的有 36 个报警, 那等于没有告警。两种情况的处置完全不同:

- structural: **应该有通道却长期零产出 → 采集器坏了, 去修**。台积电当时正是此档:
              sec_edgar 覆盖全部 US 标的(含 TSM), 但 forms 只要 8-K 而台积电只报
              6-K, 于是通道在、产出为零, 83 天无人察觉
- no_channel: 压根没有直连通道(多数中概股) → 这不是 bug, 是要不要加信源的取舍, 不报警
- thin      : 有自有披露但超过 14 天 —— 结论正在靠二手撑着
- ok        : 其余
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from radar.models import parse_iso
from radar.storage import load_items

logger = logging.getLogger(__name__)

# 判档阈值(天)
STRUCTURAL_DAYS = 30
THIN_DAYS = 14
# 回看窗口: 至少要覆盖 structural 阈值才判得出"30 天零一手"
LOOKBACK_DAYS = 45

_HKT_OFFSET = timedelta(hours=8)


def _hkt_dates(days: int) -> list[str]:
    """最近 N 天的 HKT 日期字符串, 与 archive 的分文件方式一致"""
    now = datetime.now(timezone.utc) + _HKT_OFFSET
    return [(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days)]


# 信源 → 它属于哪个覆盖标的。只有这些渠道算"该公司自己说的话"。
# 别的一手来源(比如英伟达官博)对其它标的而言仍是第三方口径。
OWN_DISCLOSURE_SOURCES = {
    "rss:openai":      "OpenAI",
    "rss:nvidia-blog": "英伟达",
    "rss:googleai":    "谷歌",
    "rss:deepmind":    "谷歌",
    "rss:msft-research": "微软",
    "rss:aws-ml":      "亚马逊",
    "rss:hf-blog":     "",          # HuggingFace 不在 coverage 内
    "rss:amd-ir":      "AMD",
    "rss:broadcom-ir": "博通",
}


def _is_primary(item) -> bool:
    return bool(getattr(item, "is_primary_source", False)) or \
        getattr(item, "credibility", "") == "high"


def own_disclosure_of(item) -> str:
    """这条是哪家公司自己发的? 不是自有披露则返回空串

    SEC 申报由采集器按 (公司, 申报) 一条条生成, 标题固定以公司名开头
    (见 collectors/sec_edgar.py), 据此还原归属。
    """
    source = getattr(item, "source", "") or ""
    if source.startswith("sec_edgar"):
        title = getattr(item, "title", "") or ""
        return title.split(" (", 1)[0].strip() if " (" in title else ""
    return OWN_DISCLOSURE_SOURCES.get(source, "")


def expected_channels(cov: dict) -> list[str]:
    """按配置, "本来就应该"直连这家公司的通道有哪些

    关键在于这是**应然**而非实然: 拿它和实际产出对照, 才分得清"通道坏了"与
    "我们从来没打算直连这家"。前者是 bug, 后者是取舍。
    """
    channels = []
    # sec_edgar 按 coverage 里 market=US 且有 ticker 的标的逐个查申报
    if cov.get("market") == "US" and cov.get("ticker"):
        channels.append("sec_edgar")
    name = cov.get("name")
    channels.extend(src for src, owner in OWN_DISCLOSURE_SOURCES.items()
                    if owner and owner == name)
    return channels


def audit(coverage: list[dict] | None = None,
          lookback_days: int = LOOKBACK_DAYS,
          now: datetime | None = None) -> dict:
    """按标的统计一手来源的新鲜度与信源多样性

    Returns:
        {
          "generated_at": iso,
          "lookback_days": N,
          "tickers": [{name, days_since_primary, days_since_any,
                       n_sources_30d, n_items_30d, level}],
          "structural": [name…],   # 便于告警直接取用
          "thin": [name…],
        }
    """
    if coverage is None:
        from radar.config import load_config
        coverage = load_config().get("coverage", [])
    if now is None:
        now = datetime.now(timezone.utc)

    _EPOCH = datetime.min.replace(tzinfo=timezone.utc)
    last_own: dict[str, datetime] = {}
    last_primary: dict[str, datetime] = {}
    last_any: dict[str, datetime] = {}
    sources_30d: dict[str, set] = defaultdict(set)
    items_30d: dict[str, int] = defaultdict(int)
    seen_sources: set[str] = set()

    horizon_30 = now - timedelta(days=30)

    for date_str in _hkt_dates(lookback_days):
        try:
            items = load_items(date_str)
        except Exception as e:
            logger.debug(f"coverage_audit: skip {date_str}: {e}")
            continue
        for it in items:
            dt = parse_iso(it.published_at) or parse_iso(it.fetched_at)
            if dt is None:
                continue
            seen_sources.add(it.source)
            owner = own_disclosure_of(it)
            if owner and dt > last_own.get(owner, _EPOCH):
                last_own[owner] = dt
            for name in it.tickers or []:
                if dt > last_any.get(name, _EPOCH):
                    last_any[name] = dt
                if _is_primary(it) and dt > last_primary.get(name, _EPOCH):
                    last_primary[name] = dt
                if dt >= horizon_30:
                    sources_30d[name].add(it.source)
                    items_30d[name] += 1

    def _age(d: dict, name: str):
        ts = d.get(name)
        return None if ts is None else max(0, (now - ts).days)

    rows = []
    for cov in coverage:
        name = cov.get("name")
        if not name:
            continue
        dso = _age(last_own, name)
        channels = expected_channels(cov)
        # 判档看自有披露 —— 用笼统的"一手来源"会把"英伟达官博提了一句台积电"
        # 算成台积电有一手覆盖, 那正是要抓的病
        if not channels:
            level = "no_channel"
        elif dso is None or dso >= STRUCTURAL_DAYS:
            level = "structural"
        elif dso >= THIN_DAYS:
            level = "thin"
        else:
            level = "ok"
        rows.append({
            "name": name,
            "market": cov.get("market", ""),
            "days_since_own": dso,
            "expected_channels": channels,
            "days_since_primary": _age(last_primary, name),
            "days_since_any": _age(last_any, name),
            "n_sources_30d": len(sources_30d.get(name, ())),
            "n_items_30d": items_30d.get(name, 0),
            "level": level,
        })

    _ORDER = {"structural": 0, "thin": 1, "no_channel": 2, "ok": 3}
    rows.sort(key=lambda r: (_ORDER.get(r["level"], 9), -r["n_items_30d"]))

    result = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lookback_days": lookback_days,
        "tickers": rows,
        "structural": [r["name"] for r in rows if r["level"] == "structural"],
        "thin": [r["name"] for r in rows if r["level"] == "thin"],
        "no_channel": [r["name"] for r in rows if r["level"] == "no_channel"],
    }
    if result["structural"]:
        # 只对 structural 报警: 那是"通道在、产出为零", 属于可修的 bug。
        # no_channel 是取舍不是故障, 混进来会让告警在所有标的上一起响。
        # 记账不拦路: 只保证它出现在人眼必经之处, 不阻断出稿。
        logger.warning(
            f"Coverage audit: {len(result['structural'])} 个标的有自有通道却 "
            f"{STRUCTURAL_DAYS} 天零产出, 疑似采集器故障 "
            f"({', '.join(result['structural'][:8])})"
        )
    return result


def for_material(audit_result: dict, max_rows: int = 12) -> list[dict]:
    """裁成喂给撰稿 LLM 的精简版: 只带 structural / thin 两档

    带全量会把素材预算吃掉, 而 ok 档对写盲区没有信息量。
    """
    rows = [r for r in audit_result.get("tickers", [])
            if r["level"] in ("structural", "thin")]
    return [
        {
            "name": r["name"],
            "level": r["level"],
            "days_since_own_disclosure": r["days_since_own"],
            "expected_channels": r["expected_channels"],
            "n_sources_30d": r["n_sources_30d"],
            "n_items_30d": r["n_items_30d"],
        }
        for r in rows[:max_rows]
    ]
