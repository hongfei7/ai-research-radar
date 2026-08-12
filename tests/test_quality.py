"""数据质量与报告版式回归测试

覆盖本次重构修掉的具体缺陷, 每个用例对应一个线上曾经出现的坏产出。
"""

import json
from pathlib import Path

import pytest

from radar.cluster import _recompute_tickers, _recompute_significance
from radar.models import Event, Item, compute_effective_score
from radar.notify import assemble, readme_index
from radar.notify.render_issue import render_issue_body, render_report_file
from radar.notify.types import DigestPayload, MacroFrame
from radar.textnorm import clean_title, clip_sentence, strip_markdown


# ================================================================
# 标题清洗
# ================================================================

@pytest.mark.parametrize("raw, expected", [
    # 站点后缀
    ("美光、高通释放ai需求利好 全球半导体板块上涨 - 财新网",
     "美光、高通释放AI需求利好 全球半导体板块上涨"),
    # 多层下划线后缀需要剥两轮
    ("Microsoft的新AI芯片Maia 300可能最早于九月亮相_新浪科技_新浪网",
     "Microsoft的新AI芯片Maia 300可能最早于九月亮相"),
    # 残留分隔符
    ("台积电最新资讯-快科技--科技改变未来", "台积电最新资讯"),
    # 话题标签尾巴(标签内含空格)
    ("长电科技：封测龙头站在先进封装风口 #长电科技 #半导体封测 #AI 算力",
     "长电科技：封测龙头站在先进封装风口"),
    # 自我重复
    ("超微ai晶片進擊 台積助攻 超微ai晶片進擊 台積助攻", "超微AI晶片進擊 台積助攻"),
    # 全小写术语
    ("谷歌第八代tpu详解：拆分训练与推理", "谷歌第八代TPU详解：拆分训练与推理"),
])
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


@pytest.mark.parametrize("raw", [
    "AI 芯片深度分析 - 上篇",     # "上篇"不是站点名, 不能剥
    "C# 教程入门指南",            # 井号不在词首空白后, 不是话题标签
    "2026 Ai生产力大会发布会",    # 已有大小写的词不动
])
def test_clean_title_leaves_legit_titles(raw):
    assert clean_title(raw) == raw


def test_clean_title_never_returns_empty():
    assert clean_title("") == ""
    assert clean_title("短") == "短"
    # 全是站点名时回退原标题, 不能清成空串
    assert clean_title("- 财新网") == "- 财新网"


def test_clip_sentence_respects_boundary():
    assert clip_sentence("第一句话很长啊。第二句话还有更多。", 12) == "第一句话很长啊。"
    assert clip_sentence("没有标点的一长串文字需要硬截断", 8).endswith("…")


def test_clip_sentence_prefers_hard_cut_over_tiny_fragment():
    """句号太靠前时宁可硬截断: 只留"第一句。"会丢掉大半信息"""
    assert clip_sentence("很短。后面是一段长得多的正文内容在这里", 14).endswith("…")


def test_strip_markdown():
    out = strip_markdown("**反向观点：**\n\n1. **周期确认偏误**：属性未提\n- 第二点")
    assert "*" not in out and "\n" not in out


# ================================================================
# 事件层收敛
# ================================================================

def test_tickers_drop_low_support():
    """7 个来源里只出现 1 次的标的不该进代表标的"""
    counts = {"英伟达": 7, "台积电": 5, "博通": 1, "小米": 1, "ASML": 1}
    assert _recompute_tickers(counts, source_count=7, max_tickers=4) == ["英伟达", "台积电"]


def test_tickers_respect_cap():
    counts = {c: 9 for c in "ABCDEFGH"}
    assert len(_recompute_tickers(counts, source_count=9, max_tickers=4)) == 4


def test_tickers_keep_one_when_support_unreachable():
    """支持度门槛没人达到时保底留最高频的一个, 而不是清空"""
    assert _recompute_tickers({"A": 1, "B": 1}, source_count=9, max_tickers=4) == ["A"]


def test_tickers_empty_input():
    assert _recompute_tickers({}, source_count=3, max_tickers=4) == []


def test_significance_can_go_down():
    """重要性不再是历史最大值: 低分单源事件就该是低分"""
    assert _recompute_significance(9, 5) == 10
    assert _recompute_significance(4, 1) == 4
    assert _recompute_significance(7, 3) == 8
    assert _recompute_significance(10, 9) == 10   # 封顶


def test_event_backfills_new_fields_from_old_state():
    """升级前的 events.json 没有 ticker_counts / max_item_score"""
    ev = Event.from_dict({
        "event_id": "evt_x", "title": "T", "summary": "S",
        "tickers": ["英伟达", "台积电"], "significance": 8,
    })
    assert ev.ticker_counts == {"英伟达": 1, "台积电": 1}
    assert ev.max_item_score == 8


def test_event_from_dict_ignores_unknown_keys():
    ev = Event.from_dict({"event_id": "e", "title": "T", "summary": "S", "legacy_field": 1})
    assert ev.event_id == "e"


# ================================================================
# 无日期条目不再被奖励
# ================================================================

def test_undated_item_is_penalised_not_rewarded():
    """搜索类信源没有发布时间, 过去按"零衰减"处理反而让它排在最前"""
    undated = Item(id="a", title="t", url="u", source="web_search", source_type="tech",
                   published_at="", fetched_at="", raw_summary="", relevance_score=8)
    assert compute_effective_score(undated, half_life_hours=4) == 4.0


# ================================================================
# 素材层: ref 与证据链
# ================================================================

def _fake_material():
    return {
        "generated_at": "2026-08-12T00:00:00Z",
        "window_hours": 24,
        "stats": {"items_ingested": 66, "events": 13},
        "events": [
            {"ref": "E1", "title": "台积电 CoWoS 扩产", "summary": "两座厂开建",
             "tickers": ["台积电", "长电科技"], "significance": 8, "source_count": 3,
             "first_seen_at": "2026-08-12T01:00:00Z", "counterpoint": "三星在追赶",
             "sources": [
                 {"title": "台积电嘉义扩产", "url": "https://a.example/1",
                  "source": "rss:36kr", "published_at": "2026-08-12T01:00:00Z",
                  "credibility": "medium", "is_primary_source": True},
             ]},
            {"ref": "E2", "title": "Nebius Q2 云收入暴增", "summary": "同比 +514%",
             "tickers": ["英伟达"], "significance": 7, "source_count": 1,
             "first_seen_at": "2026-08-12T02:00:00Z", "counterpoint": "",
             "sources": [
                 {"title": "Nebius earnings", "url": "https://b.example/2",
                  "source": "rss:techmeme", "published_at": "2026-08-12T02:00:00Z",
                  "credibility": "low", "is_primary_source": False},
             ]},
        ],
    }


def _full_payload():
    return DigestPayload.from_dict({
        "title": "AI 首席内参 | 08月12日 07:00",
        "headline": "封装是硬约束。",
        "macro": {"cycle": "扩张第三年", "constraint": "封装散热",
                  "shift_kind": "adjust", "shift": "散热权重上调"},
        "calls": [{
            "claim": "先进封装是硬约束", "direction": "台积电 ↑", "verify": "CoWoS 报价",
            "fact": "产能排至 2027。", "mechanism": "热阻取代光刻成为良率约束。",
            "inference": "溢出订单流向大陆封测。",
            "falsifier": "若 Q4 报价环比回落超 5%，本判断失效。",
            "counterpoint": "三星在追赶。", "evidence_refs": ["E1", "E99"],
        }],
        "tension": "两条判断存在张力。",
        "reviews": [{"claim": "存储拐点已过", "falsifier": "美光下调指引",
                     "status": "进展中", "basis": "美光上调指引。", "evidence_refs": ["E2"]}],
        "watchlist": [{"text": "YMTC 进入前三", "ref": "E2"},
                      {"text": "无来源的观察", "ref": "E77"}],
    })


def test_llm_material_has_no_urls():
    """撰稿 LLM 看不到 URL, 因此不可能编出链接"""
    m = _fake_material()
    assert "http" not in json.dumps(assemble.llm_material(m), ensure_ascii=False)
    # 但完整素材保留 URL 供渲染层还原
    assert "http" in json.dumps(m, ensure_ascii=False)


def test_prune_refs_drops_unknown():
    payload = _full_payload()
    dropped = payload.prune_refs({"E1", "E2"})
    assert dropped == 2                              # E99 与 watchlist 的 E77
    assert payload.calls[0].evidence_refs == ["E1"]
    assert payload.watchlist[1]["ref"] == ""


def test_validate_rejects_incomplete_call():
    payload = _full_payload()
    payload.calls[0].mechanism = ""
    with pytest.raises(ValueError, match="mechanism"):
        payload.validate(expect_calls=True)


def test_validate_rejects_call_without_evidence():
    payload = _full_payload()
    payload.prune_refs(set())                        # 所有 ref 都失效
    with pytest.raises(ValueError, match="evidence"):
        payload.validate(expect_calls=True)


def test_validate_requires_reviews_when_last_report_exists():
    payload = _full_payload()
    payload.reviews = []
    with pytest.raises(ValueError, match="review"):
        payload.validate(expect_calls=True, expect_reviews=1)


# ================================================================
# Issue 版式
# ================================================================

def test_issue_body_structure_and_links():
    material = _fake_material()
    payload = _full_payload()
    payload.prune_refs(assemble.material_refs(material))
    payload.generated_at = material["generated_at"]
    body = render_issue_body(payload, material=material)

    for section in ["## 格局", "## 本期速览", "## 上期判断回溯", "## 判断一 · ",
                    "## 张力与联动", "## 未进入判断的观察", "## 附录"]:
        assert section in body, f"缺少章节 {section}"
    for label in ["**事实**", "**机理**", "**推论**", "**证伪条件**", "**证据**", "**反面**"]:
        assert label in body

    # 证据是可点击链接, 且带信源属性标签
    assert "[台积电嘉义扩产](https://a.example/1)" in body
    assert "一手" in body
    # 幻觉 ref 不产生空证据行
    assert "E99" not in body
    # 报头统计
    assert "66 条入库" in body


def test_issue_body_has_no_dead_dashboard_link():
    """实时看板已删除, 报尾不能再指向它"""
    body = render_issue_body(_full_payload(), site_url="https://x.github.io/y",
                             material=_fake_material())
    assert "实时看板" not in body
    assert "x.github.io" not in body


def test_issue_body_generated_at_is_filled():
    """过去用 payload.generated_at(LLM 从不填), 报尾永远是 "生成于 " """
    material = _fake_material()
    body = render_issue_body(_full_payload(), material=material)
    assert f"生成于 {material['generated_at']}" in body


def test_issue_table_cells_escape_pipes():
    material = _fake_material()
    material["events"][0]["title"] = "A|B|C 事件"
    body = render_issue_body(_full_payload(), material=material)
    assert "A／B／C 事件" in body


def test_fallback_banner_is_visible():
    payload = _full_payload()
    payload.fallback = True
    assert "本期为降级稿" in render_issue_body(payload, material=_fake_material())


def test_macro_pivot_is_flagged():
    payload = _full_payload()
    payload.macro.shift_kind = "pivot"
    assert "## 格局（框架转向）" in render_issue_body(payload, material=_fake_material())


def test_empty_macro_and_reviews_omit_sections():
    """首期没有历史, 格局/回溯章节应整节消失而不是留空标题"""
    payload = _full_payload()
    payload.macro = MacroFrame()
    payload.reviews = []
    body = render_issue_body(payload, material=_fake_material())
    assert "## 格局" not in body and "## 上期判断回溯" not in body


def test_report_file_frontmatter():
    payload = _full_payload()
    body = render_issue_body(payload, material=_fake_material())
    out = render_report_file(payload, body, "https://gh/issues/1", "2026-08-12")
    assert out.startswith("---\ndate: 2026-08-12\n")
    assert "issue: https://gh/issues/1" in out
    assert "kind: morning" in out


# ================================================================
# README 索引
# ================================================================

def _seed_reports(tmp_path: Path) -> Path:
    reports = tmp_path / "reports"
    reports.mkdir()
    for d in ["2026-08-10", "2026-08-11", "2026-08-12"]:
        (reports / f"{d}.md").write_text(
            f"---\ndate: {d}\nkind: morning\nissue: https://gh/issues/{d[-2:]}\n---\n\n# body\n",
            encoding="utf-8",
        )
    (reports / "weekly-2026-W32.md").write_text("---\ndate: 2026-08-09\n---\n", encoding="utf-8")
    return reports


def test_readme_index_is_idempotent(tmp_path):
    """旧实现每轮往顶部追加一个 ---, 线上已积累 9 个空分隔符"""
    reports = _seed_reports(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        "# 标题\n\n## 最新内参\n\n- [旧](url)\n\n---\n\n---\n\n---\n\n## 系统概览\n\n正文。\n",
        encoding="utf-8",
    )
    for _ in range(10):
        readme_index.update_readme_index("https://gh/issues/12",
                                         reports_dir=reports, readme_path=readme)
    out = readme.read_text(encoding="utf-8")
    assert out.count(readme_index.INDEX_START) == 1
    assert "---\n\n---" not in out          # 空分隔符已清除且不再增生
    assert out.count("内参](") == 3          # 三期日报
    assert "weekly" not in out              # 周报不混入日报索引
    assert "正文。" in out                   # README 其余内容原样保留


def test_readme_index_second_call_is_noop(tmp_path):
    reports = _seed_reports(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("# 标题\n\n## 最新内参\n\n## 其他\n", encoding="utf-8")
    assert readme_index.update_readme_index(reports_dir=reports, readme_path=readme) is True
    assert readme_index.update_readme_index(reports_dir=reports, readme_path=readme) is False


def test_readme_index_handles_missing_reports_dir(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# 标题\n\n## 最新内参\n\n## 其他\n", encoding="utf-8")
    readme_index.update_readme_index(reports_dir=tmp_path / "nope", readme_path=readme)
    assert "_暂无归档报告_" in readme.read_text(encoding="utf-8")


# ================================================================
# M3 推理内容剥离
# ================================================================

def test_strip_reasoning_removes_think_block():
    """M3 未开 reasoning_split 时推理内联在 content 里, 不剥掉 json.loads 必失败"""
    from radar.minimax_client import strip_reasoning
    raw = '<think>我需要先分析素材……{这里甚至有花括号}</think>\n{"headline": "x"}'
    assert strip_reasoning(raw) == '{"headline": "x"}'
    assert json.loads(strip_reasoning(raw))["headline"] == "x"


def test_strip_reasoning_handles_truncated_open_tag():
    """开标签丢失时取最后一个 </think> 之后的内容"""
    from radar.minimax_client import strip_reasoning
    assert strip_reasoning('前面的推理被截断了</think>{"a": 1}') == '{"a": 1}'


def test_strip_reasoning_passthrough():
    from radar.minimax_client import strip_reasoning
    assert strip_reasoning('{"a": 1}') == '{"a": 1}'
    assert strip_reasoning("") == ""


def test_copywriter_timeout_below_http_timeout():
    """撰稿层超时必须小于 httpx 请求超时, 否则 httpx 先断开, wait_for 形同虚设"""
    from radar.minimax_client import REQUEST_TIMEOUT
    from radar.notify.copywriter import _LLM_TIMEOUT_SEC
    assert _LLM_TIMEOUT_SEC < REQUEST_TIMEOUT


def test_chat_endpoint_is_configurable(monkeypatch):
    """M3 若不在旧路由供给, 应能只靠环境变量切到 OpenAI 兼容端点"""
    from radar import minimax_client as mc
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("MINIMAX_CHAT_PATH", "chat/completions")
    c = mc.MinimaxClient(api_key="k")
    assert f"{c.base_url}{c.chat_path}" == "https://api.minimax.io/v1/chat/completions"


def test_chat_endpoint_defaults_to_legacy_route():
    from radar import minimax_client as mc
    c = mc.MinimaxClient(api_key="k")
    assert c.chat_path == "/text/chatcompletion_v2"
    assert c.model == "MiniMax-M3"
