"""notify 子系统单元测试(实现审计)

覆盖: 字节预算/UTF-8 边界/二次拆分/内容签名指纹去重/调度窗口判定/新版式规范
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from radar.models import Event, Item
from radar.notify.types import (
    DigestPayload, DigestSection, DigestItem,
    KIND_MORNING, KIND_WEEKLY, KIND_BREAKING,
)
from radar.notify import render_wecom, render_telegram
from radar.notify.scheduler import (
    decide, content_fingerprint, is_duplicate_fingerprint,
)
from radar.notify.copywriter import fallback_payload

HKT = ZoneInfo("Asia/Hong_Kong")

BRAND = {"institute": "观澜研究院", "analyst": "沈砚舟", "analyst_title": "TMT 首席分析师"}


def _cfg():
    return {
        "notify": {
            "enabled": True,
            "max_messages_per_run": 6,
            "brand": BRAND,
            "morning": {"enabled": True, "hour_hkt": 7,
                        "window_before_min": 15, "window_after_min": 60},
            "weekly": {"enabled": True, "weekday": 6, "hour_hkt": 20,
                       "window_before_min": 15},
            "breaking": {"enabled": True, "min_significance": 8,
                         "quiet_hours_breakthrough": 9, "cooldown_min": 30,
                         "max_per_run": 3, "jaccard_threshold": 0.6},
        },
    }


def _event(eid: str, sig: int, title: str = "测试事件") -> Event:
    return Event(
        event_id=eid, title=title, summary="摘要",
        tickers=["英伟达"], significance=sig,
    )


def _item(title: str) -> Item:
    return Item(
        id="x", title=title, url="https://x.com", source="t", source_type="tech",
        published_at="2026-08-12T01:00:00Z", fetched_at="2026-08-12T01:00:00Z",
        raw_summary="",
    )


def _daily_payload(n_sections: int = 5, paras_per_section: int = 3) -> DigestPayload:
    sections = [DigestSection(heading="核心判断",
                              paragraphs=["一、云厂商自研芯片外售潮改写竞争框架。依据充足。"])]
    for i in range(n_sections):
        sections.append(DigestSection(
            heading=f"维度{i}: 判断句标题",
            paragraphs=[f"第{i}维度第{j}段: 事实、含义与验证点的连贯叙述, 用于消耗字节预算。" * 2
                        for j in range(paras_per_section)],
        ))
    sections.append(DigestSection(heading="今日议程", paragraphs=["台积电法说会指引"]))
    return DigestPayload(
        kind=KIND_MORNING, title="AI 首席内参 | 08月12日 07:00",
        headline="一句话总览全局", sections=sections,
        generated_at="2026-08-12T00:00:00Z",
    )


# ================================================================
# WeCom 渲染: 字节预算与新版式
# ================================================================

def test_wecom_messages_within_byte_budget():
    msgs = render_wecom.render(_daily_payload(8, 4), site_url="https://example.com", brand=BRAND)
    assert len(msgs) > 1, "大稿件应拆分为多条"
    for m in msgs:
        assert len(m.encode("utf-8")) <= render_wecom.MAX_BYTES


def test_wecom_continuation_marker():
    msgs = render_wecom.render(_daily_payload(8, 4), site_url="https://example.com", brand=BRAND)
    assert "(续)" in msgs[1]


def test_wecom_oversize_single_block_splits():
    payload = DigestPayload(
        kind=KIND_BREAKING, title="首席快报",
        sections=[DigestSection(heading="", paragraphs=["超长段落。" * 2000])],
    )
    msgs = render_wecom.render(payload, brand=BRAND)
    for m in msgs:
        assert len(m.encode("utf-8")) <= render_wecom.MAX_BYTES


def test_wecom_brand_header():
    msgs = render_wecom.render(_daily_payload(1, 1), site_url="https://example.com", brand=BRAND)
    assert "观澜研究院 | AI 首席内参" in msgs[0]
    assert "沈砚舟" in msgs[0]


def test_wecom_zero_metadata_style():
    """新版式: 无 emoji 图标/无引用块/无评分箭头"""
    payload = _daily_payload(2, 2)
    payload.sections.append(DigestSection(heading="", items=[
        DigestItem(title="兜底条目", significance=9, summary="摘要"),
    ]))
    msgs = render_wecom.render(payload, site_url="https://example.com", brand=BRAND)
    for m in msgs:
        assert "🔥" not in m and "⚡" not in m
        assert ">" not in m, "不应使用引用块"
        assert "/10" not in m, "不应出现评分"
        assert not m.lstrip().startswith("#"), "不应使用 # 标题"
        assert "`" not in m


def test_wecom_empty_sections_dropped():
    payload = DigestPayload(
        kind=KIND_MORNING, title="AI 首席内参 | t", headline="h",
        sections=[DigestSection(heading="空章节", paragraphs=[], items=[])],
    )
    msgs = render_wecom.render(payload, brand=BRAND)
    assert all("空章节" not in m for m in msgs)


# ================================================================
# Telegram 渲染
# ================================================================

def test_telegram_within_char_budget():
    msg = render_telegram.render(_daily_payload(10, 5), site_url="https://example.com", brand=BRAND)
    assert len(msg) <= render_telegram.MAX_CHARS


def test_telegram_html_escaping():
    payload = DigestPayload(
        kind=KIND_BREAKING, title="首席快报 <b>",
        sections=[DigestSection(heading="", paragraphs=["A & B <对比>"])]
    )
    msg = render_telegram.render(payload, brand=BRAND)
    assert "&lt;对比&gt;" in msg and "&amp;" in msg


def test_telegram_full_text_daily():
    """日报 TG 版带全文(不再只截取核心观点)"""
    msg = render_telegram.render(_daily_payload(2, 1), site_url="https://example.com", brand=BRAND)
    assert "观澜研究院" in msg and "今日议程" in msg


# ================================================================
# 内容签名指纹
# ================================================================

def test_fingerprint_same_story_dedup():
    ev = _event("e1", 9)
    items = [_item("英伟达发布新一代GPU, 性能翻倍")]
    fp = content_fingerprint(ev, items)
    fp["sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev2 = _event("e2", 9, title="英伟达新 GPU 性能大幅提升")
    fp2 = content_fingerprint(ev2, [_item("英伟达发布新一代GPU, 性能翻倍提升")])
    assert is_duplicate_fingerprint(fp2, [fp], cooldown_min=30, jaccard_threshold=0.6)


def test_fingerprint_different_story_passes():
    fp = content_fingerprint(_event("e1", 9), [_item("英伟达发布新一代GPU")])
    fp["sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev2 = _event("e2", 9)
    ev2.tickers = ["台积电"]
    fp2 = content_fingerprint(ev2, [_item("台积电宣布 CoWoS 产能扩张计划")])
    assert not is_duplicate_fingerprint(fp2, [fp], cooldown_min=30, jaccard_threshold=0.6)


def test_fingerprint_cooldown_expires():
    items = [_item("英伟达发布新一代GPU, 性能翻倍")]
    fp = content_fingerprint(_event("e1", 9), items)
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    fp["sent_at"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    fp2 = content_fingerprint(_event("e2", 9), items)
    assert not is_duplicate_fingerprint(fp2, [fp], cooldown_min=30, jaccard_threshold=0.6)


# ================================================================
# 调度窗口
# ================================================================

def test_morning_fires_in_window():
    now = datetime(2026, 8, 12, 7, 0, tzinfo=HKT)
    tasks = decide(_cfg(), {"morning_last_date": ""}, [], {}, now=now)
    assert "morning" in [t.kind for t in tasks]


def test_morning_fires_once_per_day():
    now = datetime(2026, 8, 12, 7, 0, tzinfo=HKT)
    state = {"morning_last_date": "2026-08-12"}
    tasks = decide(_cfg(), state, [], {}, now=now)
    assert "morning" not in [t.kind for t in tasks]


def test_morning_late_catchup():
    now = datetime(2026, 8, 12, 8, 30, tzinfo=HKT)
    tasks = decide(_cfg(), {"morning_last_date": ""}, [], {}, now=now)
    morning = [t for t in tasks if t.kind == "morning"]
    assert morning and "补发" in morning[0].reason


def test_weekly_fires_sunday_evening():
    # 2026-08-16 是周日
    now = datetime(2026, 8, 16, 20, 5, tzinfo=HKT)
    assert now.weekday() == 6
    tasks = decide(_cfg(), {}, [], {}, now=now)
    weekly = [t for t in tasks if t.kind == "weekly"]
    assert weekly and weekly[0].slot_key.endswith("-W33")


def test_weekly_not_on_other_days():
    now = datetime(2026, 8, 12, 20, 5, tzinfo=HKT)  # 周三
    tasks = decide(_cfg(), {}, [], {}, now=now)
    assert "weekly" not in [t.kind for t in tasks]


def test_weekly_once_per_week():
    now = datetime(2026, 8, 16, 20, 5, tzinfo=HKT)
    iso_year, iso_week, _ = now.isocalendar()
    state = {"weekly_last_slot": f"{iso_year}-W{iso_week:02d}"}
    tasks = decide(_cfg(), state, [], {}, now=now)
    assert "weekly" not in [t.kind for t in tasks]


def test_breaking_quiet_hours_threshold():
    now = datetime(2026, 8, 12, 3, 0, tzinfo=HKT)
    ev8 = _event("e1", 8)
    assert not any(t.kind == "breaking" for t in decide(_cfg(), {}, [ev8], {}, now=now))
    ev9 = _event("e2", 9)
    tasks = decide(_cfg(), {}, [ev9], {"e2": [_item("英伟达新GPU")]}, now=now)
    assert any(t.kind == "breaking" for t in tasks)


def test_breaking_max_per_run():
    now = datetime(2026, 8, 12, 10, 0, tzinfo=HKT)
    events = [_event(f"e{i}", 9, title=f"事件{i}") for i in range(5)]
    items_by_event = {ev.event_id: [_item(f"完全不同的事件标题{ev.event_id}")] for ev in events}
    tasks = decide(_cfg(), {}, events, items_by_event, now=now)
    breaking = [t for t in tasks if t.kind == "breaking"][0]
    assert len(breaking.events) == 3


# ================================================================
# 兜底模板稿(散文形态)
# ================================================================

def test_fallback_payload_daily_prose():
    material = {
        "generated_at": "2026-08-12T00:00:00Z",
        "situation": "截至 08-12, 板块维持此前格局。",
        "events": [
            {"title": "事件A", "tickers": ["英伟达"], "significance": 8,
             "summary": "摘要A", "direction": {"英伟达": "positive"}},
        ],
    }
    payload = fallback_payload(KIND_MORNING, material, "08月12日 07:00")
    assert payload.fallback
    payload.validate()
    assert payload.sections[0].paragraphs, "兜底稿应为散文段落"
    msgs = render_wecom.render(payload, site_url="https://example.com", brand=BRAND)
    assert "事件A" in msgs[0] and "/10" not in msgs[0]


def test_fallback_breaking_prose():
    material = {"event": {"title": "台积电 CoWoS 扩产", "summary": "两座厂开建",
                          "tickers": ["台积电"], "significance": 9}}
    payload = fallback_payload(KIND_BREAKING, material, "08月12日 19:00")
    payload.validate()
    msgs = render_wecom.render(payload, brand=BRAND)
    assert "台积电 CoWoS 扩产" in msgs[0]
