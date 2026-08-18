"""notify 子系统单元测试(实现审计)

覆盖: 字节预算/UTF-8 边界/二次拆分/内容签名指纹去重/调度窗口判定/新版式规范
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

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

BRAND = {"institute": "Sterling 证券研究", "analyst": "Ayer", "analyst_title": "TMT 首席分析师"}


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


def _item(title: str, hours_old: float = 1) -> Item:
    # 相对当前时间, 否则固定日期会随时间推移撞上快报的内容年龄闸门
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    return Item(
        id="x", title=title, url="https://x.com", source="t", source_type="tech",
        published_at=ts, fetched_at=ts, raw_summary="",
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
    assert "Sterling 证券研究 | AI 首席内参" in msgs[0]
    assert "Ayer" in msgs[0]


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
    assert "Sterling 证券研究" in msg and "今日议程" in msg


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
# 兜底模板稿
# ================================================================

def test_fallback_payload_daily_degraded_calls():
    """晨报兜底稿降级为"只有事实层"的判断链, 仍保留 ref 以便渲染证据链接"""
    material = {
        "generated_at": "2026-08-12T00:00:00Z",
        "situation": "截至 08-12, 板块维持此前格局。",
        "events": [
            {"ref": "E1", "title": "事件A", "tickers": ["英伟达"], "significance": 8,
             "summary": "摘要A", "direction": {"英伟达": "positive"}},
        ],
    }
    payload = fallback_payload(KIND_MORNING, material, "08月12日 07:00")
    assert payload.fallback
    # 降级稿缺机理/推论/证伪, 不能按完整判断链校验
    payload.validate()
    assert payload.calls and payload.calls[0].claim == "事件A"
    assert payload.calls[0].evidence_refs == ["E1"]
    msgs = render_wecom.render(payload, site_url="https://example.com", brand=BRAND)
    assert "事件A" in msgs[0] and "/10" not in msgs[0]


def test_fallback_daily_fails_strict_call_validation():
    """降级稿不得冒充完整判断链: 按 expect_calls 校验必须失败"""
    material = {"events": [{"ref": "E1", "title": "事件A", "summary": "摘要A"}]}
    payload = fallback_payload(KIND_MORNING, material, "08月12日 07:00")
    with pytest.raises(ValueError):
        payload.validate(expect_calls=True)


def test_fallback_breaking_alert():
    material = {"event": {"ref": "E1", "title": "台积电 CoWoS 扩产", "summary": "两座厂开建",
                          "tickers": ["台积电"], "significance": 9}}
    payload = fallback_payload(KIND_BREAKING, material, "08月12日 19:00")
    payload.validate()
    assert payload.alert is not None
    assert payload.alert.evidence_ref == "E1"
    # 降级快报只有事实层, 不得冒充完整三段
    with pytest.raises(ValueError, match="alert missing"):
        payload.validate(expect_alert=True)
    msgs = render_wecom.render(payload, brand=BRAND)
    assert "台积电 CoWoS 扩产" in msgs[0]


# ================================================================
# 兜底稿的读感硬伤(2026-08-16/17 两期降级稿暴露)
# ================================================================

def test_fallback_headline_never_dangles_an_enumeration():
    """态势原文说了"两条"就得给两条 —— 截断只留下"其一"时整段弃用"""
    material = {
        "window_hours": 24,
        "situation": (
            "截至 2026-08-17 06:26 HKT，新增两条值得关注的动态：其一，据报道，"
            "OpenAI 已于上月底解散负责评估模型重大风险的 Preparedness 团队，"
            "安全治理结构变动可能影响其产品发布节奏与外部信任；其二，英伟达调整担保规模。"
        ),
        "events": [{"ref": "E1", "title": "事件A", "summary": "摘要A"}],
    }
    payload = fallback_payload(KIND_MORNING, material, "08月17日 06:48")
    assert "其一" not in payload.headline or "其二" in payload.headline
    assert payload.headline.strip()


def test_fallback_headline_keeps_usable_situation_text():
    """态势摘录本身完整时照常沿用, 不要为了保险一律换成套话"""
    material = {
        "window_hours": 24,
        "situation": "截至 08-17，算力资本开支仍是板块主线。",
        "events": [{"ref": "E1", "title": "事件A", "summary": "摘要A"}],
    }
    payload = fallback_payload(KIND_MORNING, material, "08月17日 06:48")
    assert "算力资本开支" in payload.headline


def test_fallback_claim_prefers_chinese_over_untranslated_title():
    """外文源的 title 是原文未译, 判断句该用已经中文化的 summary"""
    material = {
        "events": [{
            "ref": "E1",
            "title": "Nvidia dramatically reduces amount of OpenAI infra financing",
            "summary": "英伟达大幅下调其可能为 OpenAI 基础设施融资提供担保的金额。",
        }],
    }
    payload = fallback_payload(KIND_MORNING, material, "08月17日 06:48")
    claim = payload.calls[0].claim
    assert "英伟达" in claim
    assert not claim.startswith("Nvidia")


def test_fallback_claim_keeps_chinese_title_as_is():
    """中文标题本来就能读, 不要改道去截摘要"""
    material = {
        "events": [{"ref": "E1", "title": "英伟达据悉拟向 SB Energy 投资 30 亿美元",
                    "summary": "8月16日报道，英伟达正洽谈向软银旗下 SB Energy 投资。"}],
    }
    payload = fallback_payload(KIND_MORNING, material, "08月17日 06:48")
    assert payload.calls[0].claim == "英伟达据悉拟向 SB Energy 投资 30 亿美元"


def test_fallback_direction_never_splits_a_ticker():
    """direction 按 ticker 边界拼, 不切出半个标的名

    这组输入下旧写法 ", ".join(tickers)[:40] 会把末尾的"台湾大立光电股份"
    截成"台湾大立光电" —— 一个不存在的公司。宁可少给一个标的。
    """
    from radar.notify.copywriter import _direction_text
    tickers = ["中芯国际", "超威半导体", "台湾积体电路制造",
               "工业富联", "寒武纪", "台湾大立光电股份"]
    # 前提: 这组输入确实会触发旧写法的截断, 否则本用例形同虚设
    assert ", ".join(tickers)[:40].split(", ")[-1] not in tickers

    out = _direction_text(tickers)
    assert len(out) <= 40
    for name in out.split(", "):
        assert name in tickers


# ================================================================
# 撰稿超时后的第二次机会
# ================================================================

def _morning_material():
    return {
        "generated_at": "2026-08-17T00:00:00Z",
        "window_hours": 24,
        "situation": "算力资本开支仍是主线。",
        "events": [{"ref": "E1", "title": "事件A", "summary": "摘要A",
                    "tickers": ["英伟达"], "significance": 9}],
    }


def _good_raw():
    return {
        "title": "AI 首席内参",
        "headline": "导语",
        "macro": {"cycle": "扩张中段, 资本开支同比 +40%",
                  "trajectory": {"now": "算力供给"}, "check": "本期证据强化了供给约束"},
        "calls": [{"claim": "判断一", "fact": "事实", "mechanism": "机理",
                   "inference": "推论", "falsifier": "证伪", "evidence_refs": ["E1"]}],
    }


class _ScriptedClient:
    """按脚本决定每次 chat_json 是超时还是返回稿件, 并记录调用参数"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def chat_json(self, messages, **kwargs):
        import asyncio
        self.calls.append(kwargs)
        outcome = self.script.pop(0)
        if outcome == "timeout":
            await asyncio.sleep(3600)   # 交给外层 wait_for 掐断
        return outcome


def test_copywriter_retries_without_reasoning_after_timeout(monkeypatch):
    """首次超时不该直接认输 —— 关推理再抢一稿, 拿到的仍是完整判断链"""
    import asyncio
    from radar.notify import copywriter as cw

    monkeypatch.setattr(cw, "_LLM_TIMEOUT_BY_KIND", {cw.KIND_MORNING: 0.01})
    monkeypatch.setattr(cw, "_RETRY_TIMEOUT_SEC", 5)

    client = _ScriptedClient(["timeout", _good_raw()])
    payload = asyncio.run(cw.write_digest(
        cw.KIND_MORNING, _morning_material(), {"minimax": {"model": "m"}}, client,
    ))

    assert payload.fallback is False
    assert len(client.calls) == 2
    assert client.calls[0]["thinking"] is True
    assert client.calls[1]["thinking"] is False


def test_copywriter_falls_back_when_both_attempts_time_out(monkeypatch):
    import asyncio
    from radar.notify import copywriter as cw

    monkeypatch.setattr(cw, "_LLM_TIMEOUT_BY_KIND", {cw.KIND_MORNING: 0.01})
    monkeypatch.setattr(cw, "_RETRY_TIMEOUT_SEC", 0.01)

    client = _ScriptedClient(["timeout", "timeout"])
    payload = asyncio.run(cw.write_digest(
        cw.KIND_MORNING, _morning_material(), {"minimax": {"model": "m"}}, client,
    ))

    assert payload.fallback is True
    assert len(client.calls) == 2


def test_copywriter_does_not_retry_on_schema_failure(monkeypatch):
    """schema 不合规是内容问题, 换个推理开关也治不好, 不该白等一轮"""
    import asyncio
    from radar.notify import copywriter as cw

    client = _ScriptedClient([{"title": "x", "calls": []}])
    payload = asyncio.run(cw.write_digest(
        cw.KIND_MORNING, _morning_material(), {"minimax": {"model": "m"}}, client,
    ))

    assert payload.fallback is True
    assert len(client.calls) == 1


# ================================================================
# 降级稿不占用当日名额
# ================================================================

def test_degraded_morning_leaves_the_day_open_for_retry():
    from radar.notify.run import _record_morning_result

    state = {}
    _record_morning_result(state, "2026-08-17", is_fallback=True)
    assert state.get("morning_last_date") in (None, "")
    assert state["morning_fallback"] == {"date": "2026-08-17", "attempts": 1}


def test_successful_morning_claims_the_day_and_clears_fallback():
    from radar.notify.run import _record_morning_result

    state = {"morning_fallback": {"date": "2026-08-17", "attempts": 1}}
    _record_morning_result(state, "2026-08-17", is_fallback=False)
    assert state["morning_last_date"] == "2026-08-17"
    assert state["morning_fallback"] is None


def test_morning_stops_retrying_after_budget_exhausted():
    """认输止损: 撑满重试次数后接受降级稿, 不再一天刷屏"""
    from radar.notify.run import _record_morning_result, _MAX_FALLBACK_RETRIES

    state = {}
    for _ in range(_MAX_FALLBACK_RETRIES + 1):
        _record_morning_result(state, "2026-08-17", is_fallback=True)
    assert state["morning_last_date"] == "2026-08-17"
    assert state["morning_fallback"]["attempts"] == _MAX_FALLBACK_RETRIES + 1


def test_fallback_counter_resets_on_a_new_day():
    from radar.notify.run import _record_morning_result

    state = {"morning_fallback": {"date": "2026-08-16", "attempts": 3}}
    _record_morning_result(state, "2026-08-17", is_fallback=True)
    assert state["morning_fallback"] == {"date": "2026-08-17", "attempts": 1}


def test_morning_scheduler_reruns_while_day_unclaimed():
    """降级后不写 morning_last_date, scheduler 的补发通路就该继续排班"""
    state = {"morning_last_date": "", "morning_fallback":
             {"date": "2026-08-17", "attempts": 1}}
    now = datetime(2026, 8, 17, 7, 30, tzinfo=HKT)
    tasks = decide(_cfg(), state, [], {}, now=now)
    assert any(t.kind == "morning" for t in tasks)


def test_revision_marked_in_renderers():
    payload = DigestPayload(kind=KIND_MORNING, title="AI 首席内参 | 08月17日 07:25",
                            headline="导语", revision=True)
    payload.sections = [DigestSection(heading="要闻", paragraphs=["正文"])]
    assert "修订版" in render_wecom.render(payload, brand=BRAND)[0]
    assert "修订版" in render_telegram.render(payload, brand=BRAND)


def test_fallback_headline_does_not_end_on_a_list_separator():
    """clip_sentence 会停在分号上, 而分号意味着"后面还有" —— 别假装话说完了"""
    material = {
        "window_hours": 24,
        "situation": ("当前共有 30 个活跃事件。要点包括: DeepSeek V4 Pro 正式版发布；"
                      "Anthropic 洽购世界模型创业公司；台积电 CoWoS 再度扩产；"
                      "英伟达调整对 OpenAI 的担保规模；美光存储涨价。"),
        "events": [{"ref": "E1", "title": "事件A", "summary": "摘要A"}],
    }
    headline = fallback_payload(KIND_MORNING, material, "08月17日 06:48").headline
    assert not headline.rstrip().endswith(("；", ";", "、", "，"))


def test_execute_task_marks_revision_from_state(monkeypatch):
    """当日已推过降级稿 → 补发的这份必须在渲染前就带上 revision 标记"""
    import asyncio
    from radar.models import today_str
    from radar.notify import run as run_mod
    from radar.notify.scheduler import PushTask

    captured = {}

    def _fake_render(payload, site_url, material=None, brand=None):
        captured["revision"] = payload.revision
        return "body"

    async def _fake_write_digest(kind, material, cfg, client, current_time_hkt=""):
        return DigestPayload(kind=KIND_MORNING, title="AI 首席内参 | t",
                             headline="导语")

    monkeypatch.setattr(run_mod, "render_issue_body", _fake_render)
    monkeypatch.setattr(run_mod.copywriter, "write_digest", _fake_write_digest)
    monkeypatch.setattr(run_mod.assemble, "assemble_material",
                        lambda **kw: {"events": [{"ref": "E1"}], "stats": {}})

    task = PushTask(kind=KIND_MORNING, slot_key=today_str())
    state = {"morning_fallback": {"date": today_str(), "attempts": 1}}
    used, success, is_fallback = asyncio.run(run_mod._execute_task(
        task, _cfg(), None, None, "", state,
        dry_run=True, budget=6, now_hkt_str="08月17日 07:25",
    ))

    assert captured["revision"] is True
    assert is_fallback is False


def test_execute_task_not_a_revision_on_a_clean_day(monkeypatch):
    import asyncio
    from radar.models import today_str
    from radar.notify import run as run_mod
    from radar.notify.scheduler import PushTask

    captured = {}

    def _fake_render(payload, site_url, material=None, brand=None):
        captured["revision"] = payload.revision
        return "body"

    async def _fake_write_digest(kind, material, cfg, client, current_time_hkt=""):
        return DigestPayload(kind=KIND_MORNING, title="AI 首席内参 | t", headline="导语")

    monkeypatch.setattr(run_mod, "render_issue_body", _fake_render)
    monkeypatch.setattr(run_mod.copywriter, "write_digest", _fake_write_digest)
    monkeypatch.setattr(run_mod.assemble, "assemble_material",
                        lambda **kw: {"events": [{"ref": "E1"}], "stats": {}})

    task = PushTask(kind=KIND_MORNING, slot_key=today_str())
    asyncio.run(run_mod._execute_task(
        task, _cfg(), None, None, "", {},
        dry_run=True, budget=6, now_hkt_str="08月17日 07:00",
    ))
    assert captured["revision"] is False


def test_suppressed_revision_still_counts_as_an_attempt(monkeypatch):
    """补发轮仍是降级稿时主动不推, 但这一轮必须计数, 否则当天会一直重排"""
    import asyncio
    from radar.models import today_str
    from radar.notify import run as run_mod
    from radar.notify.scheduler import PushTask

    async def _fake_write_digest(kind, material, cfg, client, current_time_hkt=""):
        return DigestPayload(kind=KIND_MORNING, title="t", headline="h", fallback=True)

    async def _never_sent(*a, **kw):
        raise AssertionError("降级稿不该被推送第二次")

    async def _no_issue(*a, **kw):
        return None

    monkeypatch.setattr(run_mod.copywriter, "write_digest", _fake_write_digest)
    monkeypatch.setattr(run_mod.assemble, "assemble_material",
                        lambda **kw: {"events": [{"ref": "E1"}], "stats": {}})
    monkeypatch.setattr(run_mod, "_write_report_file", lambda *a, **kw: None)
    monkeypatch.setattr(run_mod, "update_readme_index", lambda *a, **kw: None)
    # GitHub 归档整条不可用: find/create 都返回空, 于是 issue_url 也是空 ——
    # 这正是"只剩推送渠道可用, 而推送又被主动按下"的最坏组合
    monkeypatch.setattr(run_mod.transport, "find_issue", _no_issue)
    monkeypatch.setattr(run_mod.transport, "create_issue", _no_issue)
    monkeypatch.setattr(run_mod.transport, "send_wecom_messages", _never_sent)
    monkeypatch.setattr(run_mod.transport, "send_telegram_html", _never_sent)

    cfg = _cfg()
    cfg["channels"] = {"wecom": {"enabled": True}, "telegram": {"enabled": True}}
    task = PushTask(kind=KIND_MORNING, slot_key=today_str())
    state = {"morning_fallback": {"date": today_str(), "attempts": 1}}

    used, success, is_fallback = asyncio.run(run_mod._execute_task(
        task, cfg, None, None, "", state,
        dry_run=False, budget=6, now_hkt_str="08月17日 07:25",
    ))

    assert is_fallback is True
    assert success is True, "主动不推不能被判成全渠道失败"
    assert used == 0


# ================================================================
# 盲区台账 —— 判断有跨期回溯, 盲区过去写完即焚
# ================================================================

def test_blindspot_ledger_records_and_counts_repeats():
    from radar.notify.state import record_blindspot

    names = ["台积电", "英伟达", "AMD"]
    state = {}
    a = record_blindspot(state, "台积电缺乏一手披露, 判断依赖二手转述",
                         "2026-08-17", coverage_names=names)
    assert a["times_seen"] == 1 and a["first_seen"] == "2026-08-17"
    # 措辞漂移但说的是同一个标的, 应当落到同一条台账
    b = record_blindspot(state, "对台积电的一手披露仍然缺失, 只能依赖二手转述",
                         "2026-08-18", coverage_names=names)
    assert b["times_seen"] == 2
    assert len(state["blindspot_ledger"]) == 1
    assert b["last_seen"] == "2026-08-18"


def test_blindspot_key_distinguishes_different_tickers():
    """同样句式换个标的就是另一个洞, 不能并成一条"""
    from radar.notify.state import record_blindspot

    names = ["台积电", "海力士"]
    state = {}
    record_blindspot(state, "台积电缺乏一手披露", "2026-08-17", coverage_names=names)
    record_blindspot(state, "海力士缺乏一手披露", "2026-08-17", coverage_names=names)
    assert len(state["blindspot_ledger"]) == 2


def test_blindspot_ledger_separates_different_gaps():
    from radar.notify.state import record_blindspot

    state = {}
    record_blindspot(state, "台积电缺乏一手披露与产能数据", "2026-08-17")
    record_blindspot(state, "评级机构对表外承诺的动作暂无任何线索", "2026-08-17")
    assert len(state["blindspot_ledger"]) == 2


def test_blindspot_escalates_after_repeats():
    """连着几期还在说同一个洞, 那是通道拿不到, 不是今天不知道"""
    from radar.notify.state import (
        record_blindspot, escalated_blindspots, BLINDSPOT_ESCALATE_TIMES,
    )
    state = {}
    for i in range(BLINDSPOT_ESCALATE_TIMES):
        record_blindspot(state, "台积电缺乏一手披露与产能数据", f"2026-08-{17+i:02d}")
    assert len(escalated_blindspots(state)) == 1


def test_blindspot_ledger_survives_state_roundtrip(monkeypatch, tmp_path):
    """_DEFAULT 陷阱回归: 不在默认字典里的键会被 load 静默丢掉"""
    from radar.notify import state as st

    path = tmp_path / "notify_state.json"
    monkeypatch.setattr(st, "_STATE_PATH", path)
    s = st.load_notify_state()
    st.record_blindspot(s, "台积电缺乏一手披露", "2026-08-17")
    st.save_notify_state(s)
    back = st.load_notify_state()
    assert back["blindspot_ledger"], "blindspot_ledger 被 load 丢掉了"
    assert back["blindspot_ledger"][0]["times_seen"] == 1


def test_blindspot_ledger_pruned():
    from radar.notify.state import record_blindspot, prune_blindspots

    # 用互不相同的中文主题, 否则只差一个数字的文本会落到同一个键
    themes = ["产能", "定价", "订单", "库存", "良率", "封装", "出口", "补贴",
              "评级", "现金", "折旧", "汇率", "关税", "电力", "人才", "诉讼",
              "并购", "回购", "分红", "指引"]
    state = {}
    for i, t in enumerate(themes):
        record_blindspot(state, f"关于{t}的信息缺口", f"2026-08-{(i % 28) + 1:02d}")
    assert len(state["blindspot_ledger"]) == len(themes)
    prune_blindspots(state, keep=12)
    assert len(state["blindspot_ledger"]) == 12


def test_empty_blindspot_ignored():
    from radar.notify.state import record_blindspot
    state = {}
    assert record_blindspot(state, "   ", "2026-08-17") == {}
    assert state.get("blindspot_ledger", []) == []


def _p(kind=KIND_MORNING):
    return DigestPayload(kind=kind, title="AI 首席内参 | t", headline="h")


def test_coverage_warning_rendered_in_appendix():
    from radar.notify.render_issue import _coverage_warning

    material = {"coverage_audit": [
        {"name": "台积电", "level": "structural", "days_since_own_disclosure": None,
         "n_items_30d": 493, "expected_channels": ["sec_edgar"]},
        {"name": "微软", "level": "thin", "days_since_own_disclosure": 14,
         "n_items_30d": 100, "expected_channels": ["sec_edgar"]},
    ]}
    lines = "\n".join(_coverage_warning(_p(), material))
    assert "覆盖度告警" in lines
    assert "台积电" in lines and "493" in lines
    assert "微软" not in lines, "thin 档不该进告警, 只有 structural 是可修的故障"


def test_coverage_warning_absent_when_clean():
    from radar.notify.render_issue import _coverage_warning
    assert _coverage_warning(_p(), {"coverage_audit": []}) == []
    assert _coverage_warning(_p(), {}) == []


def test_coverage_warning_survives_empty_appendix():
    """零事件时附录整块不出, 而覆盖度告警恰恰在素材稀薄时最该被看到"""
    from radar.notify.render_issue import render_issue_body

    material = {"events": [], "coverage_audit": [
        {"name": "台积电", "level": "structural", "days_since_own_disclosure": None,
         "n_items_30d": 493, "expected_channels": ["sec_edgar"]},
    ]}
    body = render_issue_body(_p(), material=material, brand=BRAND)
    assert "覆盖度告警" in body and "台积电" in body


def test_coverage_warning_skipped_for_breaking():
    """快报是单事件时效产品, 不背这类元信息"""
    from radar.notify.render_issue import _coverage_warning

    material = {"coverage_audit": [
        {"name": "台积电", "level": "structural", "days_since_own_disclosure": None,
         "n_items_30d": 493, "expected_channels": ["sec_edgar"]},
    ]}
    assert _coverage_warning(_p(KIND_BREAKING), material) == []
