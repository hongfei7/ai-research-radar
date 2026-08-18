"""覆盖度体检单元测试

这套指标存在的理由是一个真实事故: 台积电的一手通道(SEC)因为 forms 只配了 8-K
而结构性零产出, 83 天无人察觉。测试要锁住的正是"这种情况必须显形"。
"""

from datetime import datetime, timedelta, timezone

import pytest

from radar.models import Item
from radar.coverage_audit import (
    audit, for_material, own_disclosure_of, expected_channels,
    STRUCTURAL_DAYS, THIN_DAYS,
)

NOW = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)


def _item(source, tickers, days_ago, title="标题", cred="medium", primary=False):
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Item(
        id=f"{source}-{days_ago}-{'-'.join(tickers)}", title=title,
        url="https://x.com/a", source=source, source_type="tech",
        published_at=ts, fetched_at=ts, raw_summary="",
        tickers=list(tickers), credibility=cred, is_primary_source=primary,
    )


def _cov(*names):
    table = {
        "台积电": {"name": "台积电", "ticker": "TSM", "market": "US"},
        "英伟达": {"name": "英伟达", "ticker": "NVDA", "market": "US"},
        "AMD": {"name": "AMD", "ticker": "AMD", "market": "US"},
        "寒武纪": {"name": "寒武纪", "ticker": "688256", "market": "CN"},
        "字节跳动": {"name": "字节跳动", "ticker": "", "market": "PRIVATE"},
    }
    return [table[n] for n in names]


@pytest.fixture
def fake_archive(monkeypatch):
    """把 load_items 换成内存数据, 不碰真实归档"""
    store: dict[str, list] = {}

    def _load(date_str=None):
        return store.get(date_str, [])

    monkeypatch.setattr("radar.coverage_audit.load_items", _load)

    def put(items):
        for it in items:
            store.setdefault(it.published_at[:10], []).append(it)
    return put


def _row(result, name):
    return next(r for r in result["tickers"] if r["name"] == name)


# ================================================================
# 归属判定
# ================================================================

def test_own_disclosure_from_vendor_blog():
    assert own_disclosure_of(_item("rss:nvidia-blog", ["英伟达"], 1)) == "英伟达"


def test_own_disclosure_from_sec_filing():
    it = _item("sec_edgar:6-K", ["台积电"], 1, title="台积电 (TSM) 6-K: 月营收报告")
    assert own_disclosure_of(it) == "台积电"


def test_third_party_is_not_own_disclosure():
    """科技媒体转载不算自有披露, 无论它提到谁"""
    assert own_disclosure_of(_item("rss:tomshardware", ["台积电"], 1)) == ""


def test_vendor_blog_is_not_own_disclosure_for_others():
    """英伟达官博提到台积电 —— 对台积电而言仍是第三方口径

    这是整套指标的核心: 不做这个区分, 台积电当年就会显示成"有一手覆盖"。
    """
    it = _item("rss:nvidia-blog", ["英伟达", "台积电"], 1)
    assert own_disclosure_of(it) == "英伟达"


def test_expected_channels_for_us_and_cn():
    assert "sec_edgar" in expected_channels(_cov("台积电")[0])
    assert expected_channels(_cov("寒武纪")[0]) == []
    assert expected_channels(_cov("字节跳动")[0]) == []


# ================================================================
# 分档
# ================================================================

def test_channel_exists_but_silent_is_structural(fake_archive):
    """台积电事故的回归测试: 天天被提及, 却从无自有披露 → 必须报 structural"""
    fake_archive([_item("rss:tomshardware", ["台积电"], d) for d in range(0, 20)])
    r = audit(coverage=_cov("台积电"), now=NOW)
    row = _row(r, "台积电")
    assert row["level"] == "structural"
    assert row["days_since_own"] is None
    assert row["n_items_30d"] == 20      # 提及很多, 但一条自有披露都没有
    assert "台积电" in r["structural"]


def test_recent_own_disclosure_is_ok(fake_archive):
    fake_archive([
        _item("sec_edgar:6-K", ["台积电"], 2, title="台积电 (TSM) 6-K: 月营收"),
        _item("rss:tomshardware", ["台积电"], 1),
    ])
    assert _row(audit(coverage=_cov("台积电"), now=NOW), "台积电")["level"] == "ok"


def test_stale_own_disclosure_is_thin(fake_archive):
    fake_archive([
        _item("sec_edgar:8-K", ["AMD"], THIN_DAYS + 2, title="AMD (AMD) 8-K: x"),
    ])
    assert _row(audit(coverage=_cov("AMD"), now=NOW), "AMD")["level"] == "thin"


def test_very_stale_own_disclosure_is_structural(fake_archive):
    fake_archive([
        _item("sec_edgar:8-K", ["AMD"], STRUCTURAL_DAYS + 1, title="AMD (AMD) 8-K: x"),
    ])
    assert _row(audit(coverage=_cov("AMD"), now=NOW), "AMD")["level"] == "structural"


def test_no_channel_is_not_an_alert(fake_archive):
    """没有直连通道是取舍不是故障 —— 不加这层区分, 37 个标的会有 36 个报警"""
    fake_archive([_item("rss:36kr", ["寒武纪"], 1)])
    r = audit(coverage=_cov("寒武纪", "字节跳动"), now=NOW)
    assert _row(r, "寒武纪")["level"] == "no_channel"
    assert _row(r, "字节跳动")["level"] == "no_channel"
    assert r["structural"] == []


def test_source_diversity_counted(fake_archive):
    fake_archive([
        _item("rss:36kr", ["寒武纪"], 1),
        _item("rss:leiphone", ["寒武纪"], 2),
        _item("rss:36kr", ["寒武纪"], 3),
    ])
    assert _row(audit(coverage=_cov("寒武纪"), now=NOW), "寒武纪")["n_sources_30d"] == 2


# ================================================================
# 健壮性与素材裁剪
# ================================================================

def test_empty_archive_does_not_crash(fake_archive):
    fake_archive([])
    r = audit(coverage=_cov("台积电", "寒武纪"), now=NOW)
    assert _row(r, "台积电")["level"] == "structural"
    assert _row(r, "寒武纪")["level"] == "no_channel"


def test_for_material_keeps_only_actionable_levels(fake_archive):
    fake_archive([
        _item("rss:tomshardware", ["台积电"], 1),
        _item("rss:36kr", ["寒武纪"], 1),
        _item("rss:nvidia-blog", ["英伟达"], 1),
    ])
    rows = for_material(audit(coverage=_cov("台积电", "寒武纪", "英伟达"), now=NOW))
    names = {r["name"] for r in rows}
    assert "台积电" in names            # structural, 要让撰稿看到
    assert "寒武纪" not in names        # no_channel, 不是故障
    assert "英伟达" not in names        # ok
    assert "days_since_own_disclosure" in rows[0]
