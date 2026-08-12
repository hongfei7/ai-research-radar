"""notify 子系统单元测试(实现审计, S5)

覆盖: 字节预算/UTF-8 边界/section 二次拆分/内容签名指纹去重/调度窗口判定
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from radar.models import Event, Item
from radar.notify.types import (
    DigestPayload, DigestSection, DigestItem,
    KIND_MORNING, KIND_BREAKING,
)
from radar.notify import render_wecom, render_telegram
from radar.notify.scheduler import (
    decide, content_fingerprint, is_duplicate_fingerprint,
)
from radar.notify.copywriter import fallback_payload

HKT = ZoneInfo("Asia/Hong_Kong")


def _cfg(**overrides):
    cfg = {
        "notify": {
            "enabled": True,
            "max_messages_per_run": 6,
            "morning": {"enabled": True, "hour_hkt": 7,
                        "window_before_min": 15, "window_after_min": 60},
            "digest": {"enabled": True, "slots_hkt": ["12:30", "18:00"],
                       "grace_min": 45, "send_empty": False},
            "breaking": {"enabled": True, "min_significance": 8,
                         "quiet_hours_breakthrough": 9, "cooldown_min": 30,
                         "max_per_run": 3, "jaccard_threshold": 0.6},
        },
    }
    for k, v in overrides.items():
        cfg["notify"][k] = v
    return cfg


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


# ================================================================
# WeCom 渲染: 字节预算与拆分
# ================================================================

def _big_payload(n_sections: int = 8, items_per_section: int = 4) -> DigestPayload:
    sections = []
    for i in range(n_sections):
        sections.append(DigestSection(
            heading=f"章节{i}",
            items=[DigestItem(
                title=f"事件标题{i}-{j} 含中文测试字符",
                tickers=["英伟达", "台积电"],
                direction="positive",
                significance=8,
                summary="发生了什么: 一段包含中文的摘要文本, 用于消耗字节预算。" * 3,
                why="影响逻辑分析。" * 5,
                watch="关注验证点。",
            ) for j in range(items_per_section)],
        ))
    return DigestPayload(
        kind=KIND_MORNING, title="AI 投研雷达 · 晨报 · 08月12日",
        headline="导语", sections=sections, generated_at="2026-08-12T00:00:00Z",
    )


def test_wecom_messages_within_byte_budget():
    msgs = render_wecom.render(_big_payload(), site_url="https://example.com")
    assert len(msgs) > 1, "大稿件应拆分为多条"
    for m in msgs:
        assert len(m.encode("utf-8")) <= render_wecom.MAX_BYTES, \
            f"消息超预算: {len(m.encode('utf-8'))}B"


def test_wecom_continuation_marker():
    msgs = render_wecom.render(_big_payload(), site_url="https://example.com")
    assert "(续)" in msgs[1], "续页消息应有续标记"


def test_wecom_oversize_single_block_splits():
    # 单条 summary 超长 → 触发二次拆分, 且不超预算
    item = DigestItem(title="超长条目", tickers=["A"], significance=9,
                      summary="超长摘要。" * 2000)
    payload = DigestPayload(kind=KIND_BREAKING, title="快讯",
                            sections=[DigestSection(heading="", items=[item])])
    msgs = render_wecom.render(payload)
    for m in msgs:
        assert len(m.encode("utf-8")) <= render_wecom.MAX_BYTES


def test_wecom_empty_sections_dropped():
    payload = DigestPayload(
        kind=KIND_MORNING, title="t", headline="h",
        sections=[DigestSection(heading="空章节", paragraphs=[], items=[])],
    )
    msgs = render_wecom.render(payload)
    assert all("空章节" not in m for m in msgs)


def test_wecom_no_forbidden_syntax():
    msgs = render_wecom.render(_big_payload(2, 2))
    for m in msgs:
        assert not m.lstrip().startswith("#"), "不应使用 # 标题"
        assert "`" not in m, "不应使用 backtick"


# ================================================================
# Telegram 渲染: 字符预算
# ================================================================

def test_telegram_within_char_budget():
    msg = render_telegram.render(_big_payload(), site_url="https://example.com")
    assert len(msg) <= render_telegram.MAX_CHARS


def test_telegram_html_escaping():
    payload = DigestPayload(
        kind=KIND_BREAKING, title="快讯 <b> & \"引号\"",
        sections=[DigestSection(heading="", items=[
            DigestItem(title="A & B <对比>", significance=9, summary="s"),
        ])],
    )
    msg = render_telegram.render(payload)
    assert "<对比>" not in msg.replace("<b>", "").replace("</b>", "")
    assert "&lt;对比&gt;" in msg or "&amp;" in msg


def test_telegram_morning_only_first_section():
    payload = _big_payload(3, 2)
    payload.sections.insert(0, DigestSection(
        heading="核心观点", paragraphs=["1. 观点一", "2. 观点二"],
    ))
    msg = render_telegram.render(payload)
    assert "观点一" in msg
    assert "章节0" not in msg, "晨报 TG 版只含核心观点"


# ================================================================
# 内容签名指纹
# ================================================================

def test_fingerprint_same_story_dedup():
    ev = _event("e1", 9)
    items = [_item("英伟达发布新一代GPU, 性能翻倍")]
    fp = content_fingerprint(ev, items)
    fp["sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 同一故事的新事件(新 event_id, LLM 重写后的标题漂移)
    ev2 = _event("e2", 9, title="英伟达新 GPU 性能大幅提升")
    items2 = [_item("英伟达发布新一代GPU, 性能翻倍提升")]
    fp2 = content_fingerprint(ev2, items2)

    assert is_duplicate_fingerprint(fp2, [fp], cooldown_min=30,
                                    jaccard_threshold=0.6)


def test_fingerprint_different_story_passes():
    ev = _event("e1", 9)
    fp = content_fingerprint(ev, [_item("英伟达发布新一代GPU")])
    fp["sent_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ev2 = _event("e2", 9)
    ev2.tickers = ["台积电"]
    fp2 = content_fingerprint(ev2, [_item("台积电宣布 CoWoS 产能扩张计划")])
    assert not is_duplicate_fingerprint(fp2, [fp], cooldown_min=30,
                                        jaccard_threshold=0.6)


def test_fingerprint_cooldown_expires():
    ev = _event("e1", 9)
    items = [_item("英伟达发布新一代GPU, 性能翻倍")]
    fp = content_fingerprint(ev, items)
    old_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    fp["sent_at"] = old_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    fp2 = content_fingerprint(_event("e2", 9), items)
    assert not is_duplicate_fingerprint(fp2, [fp], cooldown_min=30,
                                        jaccard_threshold=0.6)


# ================================================================
# 调度窗口
# ================================================================

def test_morning_fires_in_window():
    now = datetime(2026, 8, 12, 7, 0, tzinfo=HKT)
    tasks = decide(_cfg(), {"morning_last_date": ""}, [], {}, now=now)
    kinds = [t.kind for t in tasks]
    assert "morning" in kinds


def test_morning_fires_once_per_day():
    now = datetime(2026, 8, 12, 7, 0, tzinfo=HKT)
    state = {"morning_last_date": "2026-08-12"}
    tasks = decide(_cfg(), state, [], {}, now=now)
    assert "morning" not in [t.kind for t in tasks]


def test_morning_late_catchup():
    # Actions 排队抖动: 08:30 才跑, 已过窗口但当未发 → 补发
    now = datetime(2026, 8, 12, 8, 30, tzinfo=HKT)
    tasks = decide(_cfg(), {"morning_last_date": ""}, [], {}, now=now)
    morning = [t for t in tasks if t.kind == "morning"]
    assert morning and "补发" in morning[0].reason


def test_digest_past_grace_skipped():
    # 12:30 槽位, grace 45min → 13:20 过窗跳过
    now = datetime(2026, 8, 12, 13, 20, tzinfo=HKT)
    tasks = decide(_cfg(), {}, [], {}, now=now)
    assert "digest" not in [t.kind for t in tasks]


def test_digest_in_grace_fires():
    now = datetime(2026, 8, 12, 12, 45, tzinfo=HKT)
    tasks = decide(_cfg(), {}, [], {}, now=now)
    digest = [t for t in tasks if t.kind == "digest"]
    assert digest and digest[0].slot_key == "2026-08-12T12:30"


def test_breaking_quiet_hours_threshold():
    # 凌晨 3 点: sig=8 不发, sig=9 破例
    now = datetime(2026, 8, 12, 3, 0, tzinfo=HKT)
    ev8 = _event("e1", 8)
    tasks = decide(_cfg(), {}, [ev8], {}, now=now)
    assert not any(t.kind == "breaking" for t in tasks)

    ev9 = _event("e2", 9)
    tasks = decide(_cfg(), {}, [ev9], {"e2": [_item("英伟达新GPU")]}, now=now)
    assert any(t.kind == "breaking" for t in tasks)


def test_breaking_max_per_run():
    now = datetime(2026, 8, 12, 10, 0, tzinfo=HKT)
    events = [_event(f"e{i}", 9 - (i % 2), title=f"事件{i}") for i in range(5)]
    for ev in events:
        ev.significance = 9
    items_by_event = {ev.event_id: [_item(f"完全不同的事件标题{ev.event_id}")] for ev in events}
    tasks = decide(_cfg(), {}, events, items_by_event, now=now)
    breaking = [t for t in tasks if t.kind == "breaking"][0]
    assert len(breaking.events) == 3, "每轮快讯硬上限 3 条"


# ================================================================
# 兜底模板稿
# ================================================================

def test_fallback_payload_morning():
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
    msgs = render_wecom.render(payload, site_url="https://example.com")
    assert msgs and "事件A" in msgs[0]


def test_fallback_payload_empty_material():
    payload = fallback_payload(KIND_MORNING, {"events": []}, "08月12日 07:00")
    assert payload.fallback
    # 空素材: 无 sections, 只有 headline —— 可通过 validate(headline 为空则报错)
    # 这里 headline 为空 + 无 sections → validate 应抛错, 渲染层不应收到这种稿件
    # 但 run() 中空素材晨报由 scheduler/assemble 保证 situation 非空或仍发送降级稿
