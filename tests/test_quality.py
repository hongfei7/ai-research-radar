"""数据质量与报告版式回归测试

覆盖本次重构修掉的具体缺陷, 每个用例对应一个线上曾经出现的坏产出。
"""

import json
from datetime import datetime, timezone
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
        "macro": {"cycle": "扩张第三年",
                  "trajectory": {"past": "晶圆制程产能", "now": "封装散热",
                                 "next": "数据中心电力", "trigger": "若电力审批成为交付瓶颈"},
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


# ================================================================
# 标题清洗与索引页过滤(补漏)
# ================================================================

@pytest.mark.parametrize("raw, expected", [
    ("台积电_台积电最新动态_IT之家", "台积电_台积电最新动态"),
    ("测试标题-快科技", "测试标题"),
    ("英伟达业绩超预期 - 半导体行业观察", "英伟达业绩超预期"),
])
def test_clean_title_more_site_suffixes(raw, expected):
    assert clean_title(raw) == expected


@pytest.mark.parametrize("url", [
    "https://www.ithome.com/tags/%E5%8F%B0%E7%A7%AF%E7%94%B5",
    "https://www.36kr.com/topics/ai",
    "https://example.com/category/semiconductors",
    "https://example.com/search?q=nvidia",
    "https://example.com/",
    "",
])
def test_index_pages_are_rejected(url):
    from radar.textnorm import is_index_page
    assert is_index_page(url)


@pytest.mark.parametrize("url", [
    "https://techcrunch.com/2026/07/09/metas-new-ai-chips/",
    "https://www.toutiao.com/article/7543833778737660431/",
    "https://xueqiu.com/4341620579/404593520",
])
def test_article_pages_are_kept(url):
    from radar.textnorm import is_index_page
    assert not is_index_page(url)


# ================================================================
# 格局的时间轨迹
# ================================================================

def _payload_with_trajectory(**traj):
    p = _full_payload()
    from radar.notify.types import MacroTrajectory
    p.macro.trajectory = MacroTrajectory(**traj)
    return p


def test_macro_trajectory_renders_arc():
    p = _payload_with_trajectory(past="晶圆制程产能", now="先进封装与散热",
                                 next="数据中心电力", trigger="若单机柜功率密度超 130kW")
    body = render_issue_body(p, material=_fake_material())
    assert "**主线轨迹** 过去：晶圆制程产能 → **当下：先进封装与散热** → 未来：数据中心电力" in body
    assert "**切换信号** 若单机柜功率密度超 130kW" in body


def test_macro_trajectory_without_future_keeps_arc_intact():
    """素材不足以外推时只给当下, 箭头不能断成半截"""
    p = _payload_with_trajectory(now="先进封装与散热")
    body = render_issue_body(p, material=_fake_material())
    assert "过去：未标注 → **当下：先进封装与散热**" in body
    assert "切换信号" not in body


def test_macro_requires_current_constraint():
    p = _payload_with_trajectory(past="晶圆制程产能", next="数据中心电力")
    with pytest.raises(ValueError, match="current constraint"):
        p.validate(expect_calls=True)


def test_macro_accepts_legacy_constraint_field():
    """LLM 漏给 trajectory 只给 constraint 时降级为"只有当下"而不是整节丢失"""
    from radar.notify.types import MacroFrame
    m = MacroFrame.from_dict({"cycle": "扩张第三年", "constraint": "封装散热"})
    assert m.trajectory.now == "封装散热"
    assert not m.is_empty()


def test_macro_trajectory_round_trips_through_state():
    p = _payload_with_trajectory(past="A", now="B", next="C", trigger="D")
    from radar.notify.types import MacroFrame
    assert MacroFrame.from_dict(p.macro.to_state()).trajectory.to_state() == {
        "past": "A", "now": "B", "next": "C", "trigger": "D"}


# ================================================================
# 聚合帖过滤
# ================================================================

@pytest.mark.parametrize("title", [
    "氪星晚报｜证监会同意宇树科技科创板IPO注册；Meta带崩科技股",
    "氪星早报｜苹果折叠屏已在量产",
    "景嘉微最新核心消息汇总",
    "本周要闻回顾：先进封装产能",
    "AI 芯片今日热点盘点",
])
def test_digest_titles_are_filtered(title):
    from radar.textnorm import is_digest_title
    assert is_digest_title(title)


@pytest.mark.parametrize("title", [
    "英伟达成立一周年庆典",            # "一周"不能命中
    "台积电业绩超预期",
    "SK海力士重启中国NAND闪存生产基地建设",
    "半导体行业观察：先进封装深度报告",  # "报告"不是"周报"
    "美光上调全年指引",
])
def test_normal_titles_survive_digest_filter(title):
    from radar.textnorm import is_digest_title
    assert not is_digest_title(title)


def test_digest_patterns_are_configurable():
    from radar.textnorm import is_digest_title
    assert is_digest_title("某某特刊", ["特刊"])
    assert not is_digest_title("氪星晚报｜xxx", ["特刊"])


# ================================================================
# 快报版面
# ================================================================

def _breaking_material():
    return {
        "generated_at": "2026-08-13T01:00:00Z",
        "event": {
            "ref": "E1", "title": "鸿海Q4出货英伟达Vera Rubin",
            "summary": "已量产", "source_count": 2,
            "tickers": ["中芯国际", "长电科技", "英伟达", "台积电"],
            "direction": {"长电科技": "positive", "英伟达": "negative",
                          "中芯国际": "neutral"},
            "sources": [
                {"title": "鸿海Q4出货Vera Rubin", "url": "https://a.example/1",
                 "source": "rss:36kr", "published_at": "2026-08-13T00:00:00Z",
                 "credibility": "medium", "is_primary_source": True},
            ],
        },
        "items": [{"title": "t", "url": "https://a.example/1", "cn_summary": "s"}],
    }


def _breaking_payload(**overrides):
    from radar.notify.types import KIND_BREAKING
    alert = {"summary": "鸿海Q4开始出货Vera Rubin平台。", "why": "供给节奏前移一个季度。",
             "watch": "Q4鸿海月度营收中AI服务器占比。", "evidence_ref": "E1"}
    alert.update(overrides)
    return DigestPayload.from_dict(
        {"title": "首席快报 | 08月13日 09:20", "alert": alert}, kind=KIND_BREAKING)


def test_breaking_material_exposes_events_for_generic_helpers():
    """快报素材要能直接用 material_refs / sources_by_ref, 不另起一套协议"""
    m = _breaking_material()
    m["events"] = [m["event"]]
    assert assemble.material_refs(m) == {"E1"}
    assert assemble.sources_by_ref(m)["E1"][0]["url"] == "https://a.example/1"


def test_llm_material_strips_urls_from_breaking_too():
    """过去只在日报路径剥 URL, 等于给快报留着幻觉链接的通道"""
    m = _breaking_material()
    m["events"] = [m["event"]]
    assert "http" not in json.dumps(assemble.llm_material(m), ensure_ascii=False)


def test_breaking_validate_requires_three_parts():
    with pytest.raises(ValueError, match="watch"):
        _breaking_payload(watch="").validate(expect_alert=True)
    with pytest.raises(ValueError, match="why"):
        _breaking_payload(why="").validate(expect_alert=True)


def test_breaking_prunes_unknown_evidence_ref():
    p = _breaking_payload(evidence_ref="E99")
    assert p.prune_refs({"E1"}) == 1
    assert p.alert.evidence_ref == ""


def test_ticker_line_omits_neutral_arrows_and_fronts_directional():
    from radar.notify.brand import ticker_line
    line = ticker_line(["中芯国际", "长电科技", "英伟达", "台积电"],
                       {"长电科技": "positive", "英伟达": "negative",
                        "中芯国际": "neutral"})
    # 上限两个: 三个以上在手机上会折行
    assert line == "长电科技 ↑｜英伟达 ↓"


def test_ticker_line_without_direction_data():
    from radar.notify.brand import ticker_line
    assert ticker_line(["英伟达", "台积电"]) == "英伟达｜台积电"
    assert ticker_line([]) == ""


BRAND = {"institute": "Sterling 证券研究", "analyst": "Ayer",
         "analyst_title": "TMT 首席分析师"}


def test_breaking_wecom_layout():
    from radar.notify import render_wecom
    m = _breaking_material(); m["events"] = [m["event"]]
    msgs = render_wecom.render(_breaking_payload(), "", "https://gh/issues/40",
                               brand=BRAND, material=m)
    body = msgs[0]
    # 抬头两行: 品牌署名 + 标的与时间
    assert body.startswith("**Sterling 证券研究 · Ayer 首席快报**\n"
                           "长电科技 ↑｜英伟达 ↓ · 08月13日 09:20")
    assert "**判断**" in body and "**盯**" in body
    # 采集器 id 是系统实现细节, 对读者零价值, 必须换成出版方
    assert "web_search" not in body and "rss:" not in body
    assert "36氪" in body
    assert "[原文](https://a.example/1)" in body
    assert "[今日汇总](https://gh/issues/40)" in body
    # 汇总链接只出现一次(不与通用报尾重复)
    assert body.count("https://gh/issues/40") == 1


def test_wecom_never_emits_horizontal_rules():
    """企业微信 markdown v1 不渲染分割线, "---" 会原样显示成三个减号,
    还把手机通知栏的预览位置占掉。层级一律靠空行与加粗表达。
    """
    from radar.notify import render_wecom
    m = _breaking_material(); m["events"] = [m["event"]]
    for payload, material in ((_breaking_payload(), m), (_full_payload(), _fake_material())):
        body = render_wecom.render(payload, "", "https://gh/issues/40",
                                   brand=BRAND, material=material)[0]
        assert "---" not in body.split("\n")


def test_alert_body_reaches_notification_preview():
    """正文要顶到第 3 行 —— 通知栏只显示头两三行, 之前被 "---" 占掉了"""
    from radar.notify import render_wecom
    m = _breaking_material(); m["events"] = [m["event"]]
    lines = render_wecom.render(_breaking_payload(), "", "", brand=BRAND,
                                material=m)[0].split("\n")
    assert lines[0].startswith("**Sterling")
    assert "08月13日 09:20" in lines[1]
    assert lines[3] == "鸿海Q4开始出货Vera Rubin平台。"


def test_footer_shows_cross_source_count():
    """交叉验证强度本身就是信号, 单源与三源同报的可信度差很多"""
    from radar.notify import render_wecom
    m = _breaking_material()
    m["event"]["sources"].append({
        "title": "另一家报道", "url": "https://b.example/2", "source": "rss:techmeme",
        "published_at": "2026-08-13T00:30:00Z", "credibility": "low",
        "is_primary_source": False})
    m["events"] = [m["event"]]
    body = render_wecom.render(_breaking_payload(), "", "", brand=BRAND, material=m)[0]
    assert "36氪 +1家" in body


def test_footer_omits_count_for_single_source():
    from radar.notify import render_wecom
    m = _breaking_material(); m["events"] = [m["event"]]
    body = render_wecom.render(_breaking_payload(), "", "", brand=BRAND, material=m)[0]
    assert "36氪" in body and "+" not in body.split("36氪")[1][:6]


def test_breaking_telegram_layout():
    from radar.notify import render_telegram
    m = _breaking_material(); m["events"] = [m["event"]]
    out = render_telegram.render(_breaking_payload(), "", "https://gh/issues/40",
                                 brand=BRAND, material=m)
    assert out.startswith("<b>Sterling 证券研究 · Ayer 首席快报</b>\n"
                          "长电科技 ↑｜英伟达 ↓ · 08月13日 09:20")
    assert '<a href="https://a.example/1">原文</a>' in out
    assert out.count("https://gh/issues/40") == 1


def test_breaking_issue_body_is_lightweight():
    """快报归档进当日汇总 Issue 的评论, 外层已有 ### 时间, 不能再套 H1"""
    m = _breaking_material(); m["events"] = [m["event"]]
    body = render_issue_body(_breaking_payload(), material=m)
    assert not body.startswith("#")
    assert "## 附录" not in body          # 单事件不需要单行附录
    assert "[鸿海Q4出货Vera Rubin](https://a.example/1)" in body


def test_alert_footer_shows_publisher_not_collector_id():
    """报尾必须报"谁发的", 不能露出 web_search / rss:36kr 这类采集器标识"""
    from radar.notify.brand import publisher_name
    assert publisher_name({"source": "rss:36kr"}) == "36氪"
    assert publisher_name({"source": "arxiv:cs.AI"}) == "arXiv"
    # 搜索类采集器没有出版方字段, 从域名反查
    assert publisher_name(
        {"source": "web_search", "url": "https://news.qq.com/rain/a/1"}) == "腾讯新闻"
    # 未收录的域名退化为域名本身, 也好过 "web_search"
    assert publisher_name(
        {"source": "web_search", "url": "https://aichipfront.com/x"}) == "aichipfront.com"


def test_caveat_label_only_when_informative():
    """二手是默认值, 不该占位; 低可信是警示, 一手是加分"""
    from radar.notify.brand import caveat_label
    assert caveat_label({"credibility": "low"}) == "低可信"
    assert caveat_label({"credibility": "high", "is_primary_source": True}) == "一手"
    assert caveat_label({"credibility": "medium", "is_primary_source": False}) == ""


def test_low_credibility_is_visually_weak_in_wecom():
    """可信度是脚注, 不能和判断抢注意力"""
    from radar.notify import render_wecom
    m = _breaking_material(); m["events"] = [m["event"]]
    m["event"]["sources"][0].update(credibility="low", is_primary_source=False)
    body = render_wecom.render(_breaking_payload(), "", "", brand=BRAND, material=m)[0]
    assert '<font color="comment">低可信</font>' in body


def test_alert_budget_clips_at_sentence_boundary():
    """prompt 写了字数上限但 LLM 会无视, 代码兜底且不切半句"""
    from radar.notify.copywriter import _enforce_alert_budget
    p = _breaking_payload(
        summary="第一句话讲清楚发生了什么事情很重要。" * 6,
        why="判断句。" * 30,
    )
    _enforce_alert_budget(p)
    assert len(p.alert.summary) <= 110
    assert len(p.alert.why) <= 80
    # 按句号边界裁, 不留半句
    assert p.alert.summary.endswith("。")


def test_alert_budget_leaves_short_text_alone():
    from radar.notify.copywriter import _enforce_alert_budget
    p = _breaking_payload()
    before = (p.alert.summary, p.alert.why, p.alert.watch)
    _enforce_alert_budget(p)
    assert (p.alert.summary, p.alert.why, p.alert.watch) == before


# ================================================================
# 时效性: 无日期不等于新鲜
# ================================================================

def _mk_item(iid="i1", published_at="", source="rss:36kr", score=8):
    from radar.models import utcnow_iso
    return Item(id=iid, title="t", url=f"https://x/{iid}", source=source,
                source_type="tech", published_at=published_at,
                fetched_at=utcnow_iso(), raw_summary="s", relevance_score=score)


def test_undated_items_are_dropped_at_collection():
    """无日期条目此前无条件放行, 三个月前的 SEO 洗稿文因此成了突发快报"""
    import asyncio
    from datetime import timedelta
    import main as radar_main
    from radar.models import utcnow_iso

    fresh = _mk_item("fresh", utcnow_iso())
    stale = _mk_item("stale",
                     (datetime.now(timezone.utc) - timedelta(days=90)).isoformat())
    undated = _mk_item("undated", "", source="web_search")

    cfg = {"runtime": {"rolling_window_hours": 8}, "coverage": [],
           "sources": {"tech": [{"id": "rss:36kr", "type": "rss", "params": {}},
                                {"id": "web_search", "type": "web_search", "params": {}}],
                       "market": []}}

    class _Stub:
        async def fetch(self, sid, params):
            return [fresh, stale, undated] if sid == "rss:36kr" else []

    orig_map = dict(radar_main.COLLECTOR_MAP)
    orig_dedup = radar_main.DedupStore
    radar_main.COLLECTOR_MAP.update({"rss": _Stub(), "web_search": _Stub()})

    class _NoDedup:
        def filter_new(self, ids): return ids
        def close(self): pass
    radar_main.DedupStore = _NoDedup
    try:
        kept = asyncio.run(radar_main.collect_all(cfg))
    finally:
        radar_main.COLLECTOR_MAP.clear(); radar_main.COLLECTOR_MAP.update(orig_map)
        radar_main.DedupStore = orig_dedup

    assert [i.id for i in kept] == ["fresh"]


def test_assemble_excludes_undated_items(tmp_path, monkeypatch):
    """素材层与采集层保持同一不变式"""
    from radar.notify import assemble as asm
    monkeypatch.setattr(asm, "load_items", lambda d: [
        _mk_item("dated", datetime.now(timezone.utc).isoformat()),
        _mk_item("undated", ""),
    ])
    assert [i.id for i in asm.load_window_items(24)] == ["dated"]


# ================================================================
# 快报内容年龄闸门
# ================================================================

def _fresh_event(hours_old: float):
    from datetime import timedelta
    from radar.notify.scheduler import _content_is_fresh
    ev = Event(event_id="e1", title="T", summary="S", significance=9)
    published = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    return _content_is_fresh(ev, {"e1": [_mk_item("i1", published)]}, 24)


def test_breaking_allows_fresh_content():
    assert _fresh_event(3) is True


def test_breaking_blocks_stale_content():
    """SEC 窗口 72h, 三天前的 8-K 不该以"突发"名义推送"""
    assert _fresh_event(72) is False


def test_breaking_blocks_when_no_publish_time():
    """拿不出新鲜度证据就不推"""
    from radar.notify.scheduler import _content_is_fresh
    ev = Event(event_id="e1", title="T", summary="S", significance=9)
    assert _content_is_fresh(ev, {"e1": [_mk_item("i1", "")]}, 24) is False
    assert _content_is_fresh(ev, {}, 24) is False


# ================================================================
# 真实首报时间
# ================================================================

def test_event_tracks_earliest_publish_time():
    from radar.cluster import _earlier_iso
    assert _earlier_iso("2026-08-13T00:00:00Z", "2026-05-04T00:00:00Z") \
        == "2026-05-04T00:00:00Z"
    assert _earlier_iso("", "2026-05-04T00:00:00Z") == "2026-05-04T00:00:00Z"
    assert _earlier_iso("2026-08-13T00:00:00Z", "") == "2026-08-13T00:00:00Z"


def test_event_backfills_first_published_from_first_seen():
    ev = Event.from_dict({"event_id": "e", "title": "T", "summary": "S",
                          "first_seen_at": "2026-05-28T00:00:00Z"})
    assert ev.first_published_at == "2026-05-28T00:00:00Z"


def test_appendix_shows_publish_date_not_ingest_date():
    """5 月的新闻 8 月才入库, 附录该报 05-04 而不是 08-12"""
    material = _fake_material()
    material["events"][0]["first_published_at"] = "2026-05-04T00:00:00Z"
    material["events"][0]["first_seen_at"] = "2026-08-12T00:00:00Z"
    body = render_issue_body(_full_payload(), material=material)
    row = next(l for l in body.splitlines() if "台积电 CoWoS 扩产" in l and l.startswith("|"))
    assert "| 05-04 |" in row       # 发布时间
    assert "| 08-12 |" not in row   # 入库时间不该出现


# ================================================================
# 聚类: 重叠系数取代 Jaccard
# ================================================================

def _cluster_items(specs):
    """跑真实聚类路径; specs = [(cn_summary, tickers, themes)]"""
    import asyncio
    from radar.cluster import ClusterEngine
    from radar.config import load_config

    items = []
    for i, (summary, tickers, themes) in enumerate(specs):
        it = _mk_item(f"c{i}", datetime.now(timezone.utc).isoformat())
        it.title = summary[:30]
        it.cn_summary = summary
        it.tickers = list(tickers)
        it.themes = list(themes)
        items.append(it)

    class _NoLLM:
        async def chat_json(self, *a, **k):
            raise RuntimeError("replay without LLM")

    _, events = asyncio.run(ClusterEngine(_NoLLM(), load_config()).cluster(items, {}))
    return events


# 取自 2026-08-13 真实归档: DeepSeek V4 Pro 发布当天的三条报道
_DS_EN = ("DeepSeek V4 Pro 0813 released with 1M context window, "
          "input priced at 3 yuan per million tokens and cache hit at 0.025 yuan.")
_DS_CN = ("DeepSeek V4 Pro 正式版上线，支持 1M 上下文，输入价 3 元每百万 Token，"
          "缓存命中 0.025 元，实测能力直逼头部闭源模型，V4 Flash 登顶全球 Token 调用量。")
_DIGEST = ("星巴克中国否认为降成本取消 14 薪；DeepSeek V4 Pro 正式版上线；"
           "阿里最年轻 P10 林俊旸创业，新公司估值 20 亿美元；前 7 个月期货成交额增长。")
_THEMES = ["model_capability", "ai_monetization", "compute_demand"]


def test_same_story_across_languages_merges():
    """短英文报道与长中文报道讲同一件事, 必须合并成一个事件

    Jaccard 时代它们被拆成两个单源事件, 各自 sig=7 够不到快报阈值 —— 线上
    DeepSeek V4 Pro 发布当天就是这样漏报的。
    """
    events = _cluster_items([
        (_DS_EN, ["DeepSeek"], _THEMES),
        (_DS_CN, ["DeepSeek"], _THEMES),
    ])
    assert len(events) == 1
    ev = next(iter(events.values()))
    assert ev.source_count == 2


def test_merged_sources_lift_significance_over_breaking_threshold():
    """三源合并后 sig 由 7 升到 8, 正好越过快报门槛"""
    events = _cluster_items([
        (_DS_EN, ["DeepSeek"], _THEMES),
        (_DS_CN, ["DeepSeek"], _THEMES),
        (_DS_CN + " API 已开始灰度推送。", ["DeepSeek"], _THEMES),
    ])
    ev = next(iter(events.values()))
    assert ev.source_count == 3
    assert ev.significance >= 8


def test_digest_post_stays_separate_from_real_story():
    """聚合帖词汇分布接近语料均值, 不能让它并进真报道"""
    events = _cluster_items([
        (_DS_EN, ["DeepSeek"], _THEMES),
        (_DIGEST, ["DeepSeek", "阿里巴巴"], _THEMES),
    ])
    assert len(events) == 2


def test_short_titles_need_absolute_overlap():
    """重叠系数对极短文本敏感, 共享一两个 token 不该算同题"""
    events = _cluster_items([
        ("Grok 4.6", ["xAI"], ["model_capability"]),
        ("Grok teammate", ["xAI"], ["model_capability"]),
    ])
    assert len(events) == 2


# ================================================================
# 聚合帖: 多话题结构判据
# ================================================================

@pytest.mark.parametrize("title", [
    "8点1氪丨星巴克中国否认取消14薪；DeepSeek V4 Pro正式版上线；林俊旸创业估值20亿",
    "科技早参丨英特尔增发200亿美元；期货成交额增长38%；汽车出口延续强劲",
])
def test_multi_topic_titles_detected_without_keyword(title):
    """穷举栏目名追不完(线上漏过 8点1氪), 结构特征才稳"""
    from radar.textnorm import is_digest_title
    assert is_digest_title(title, patterns=[])   # 关键词列表为空也能识别


@pytest.mark.parametrize("title", [
    "英伟达发布B300；性能较上代提升2倍",
    "高盛：上调寒武纪评级；下调浪潮信息",
    "实测 DeepSeek V4 Pro 正式版：能力直逼 Fable 5",
])
def test_two_segment_titles_are_not_digests(title):
    from radar.textnorm import is_digest_title
    assert not is_digest_title(title, patterns=[])


# ================================================================
# 裁剪不切半句
# ================================================================

def test_clip_keeps_whole_sentence_when_no_boundary():
    """线上出现过"…谁有自研芯片谁就能留住…", 被砍断的判断比略长更糟"""
    long_one = "利润池向芯片端上移意味着服务器厂商的议价能力将持续下滑而芯片设计公司吃到全部替代红利。"
    out = clip_sentence(long_one, 30, hard=False)
    assert out == long_one          # 只有一句, 整句保留
    assert not out.endswith("…")


def test_clip_drops_trailing_sentence_within_budget():
    out = clip_sentence("第一句足够长可以单独成立。第二句应当被丢弃。", 20, hard=False)
    assert out == "第一句足够长可以单独成立。"


def test_alert_budget_never_emits_fragment():
    from radar.notify.copywriter import _enforce_alert_budget
    p = _breaking_payload(why="没有任何句末标点的超长判断" * 8)
    _enforce_alert_budget(p)
    assert not p.alert.why.endswith("…")


def test_breaking_title_gets_time_from_system_not_llm():
    """prompt 让 LLM 输出的 title 是"首席快报"(无时间), 抬头因此永远缺时间"""
    import asyncio
    from radar.notify import copywriter
    from radar.notify.types import KIND_BREAKING

    material = _breaking_material(); material["events"] = [material["event"]]

    class _FakeClient:
        async def chat_json(self, *a, **k):
            return {"title": "首席快报", "alert": {
                "summary": "鸿海Q4出货。", "why": "供给前移。",
                "watch": "月度营收占比。", "evidence_ref": "E1"}}

    p = asyncio.run(copywriter.write_digest(
        KIND_BREAKING, material, {"minimax": {"model": "m"}}, _FakeClient(),
        current_time_hkt="08月13日 09:20"))
    assert p.title == "首席快报 | 08月13日 09:20"

    from radar.notify import render_wecom
    body = render_wecom.render(p, "", "", brand=BRAND, material=material)[0]
    assert "08月13日 09:20" in body


# ================================================================
# 抬头只留有方向的标的
# ================================================================

def test_header_drops_directionless_tickers():
    """同一行里有的带箭头有的光秃秃, 看起来像渲染坏了; 没有方向判断的
    标的挤在只能放两个的抬头里也帮不了决策"""
    from radar.notify.brand import ticker_line
    line = ticker_line(["DeepSeek", "AMD", "腾讯"],
                       {"DeepSeek": "positive", "AMD": "neutral"})
    assert line == "DeepSeek ↑"


def test_header_keeps_both_directional():
    from radar.notify.brand import ticker_line
    line = ticker_line(["寒武纪", "浪潮信息"],
                       {"寒武纪": "positive", "浪潮信息": "negative"})
    assert line == "寒武纪 ↑｜浪潮信息 ↓"


def test_header_falls_back_to_bare_tickers():
    """一个方向都没有时不能让抬头空掉"""
    from radar.notify.brand import ticker_line
    assert ticker_line(["英伟达", "台积电"], {"英伟达": "neutral"}) == "英伟达｜台积电"


# ================================================================
# ticker 收敛到新闻主体
# ================================================================

def _processor():
    from radar.config import load_config
    from radar.processor import Processor
    cfg = load_config()
    p = Processor.__new__(Processor)
    p.cfg = cfg
    p._max_tickers = cfg["scoring"]["max_tickers_per_item"]
    p._valid_tickers = {c["name"] for c in cfg["coverage"]}
    p._alias_to_name = {}
    p._valid_themes = {t["key"] for t in cfg["themes"]}
    p._names_and_aliases = {
        c["name"]: tuple([c["name"], *(c.get("aliases") or [])]) for c in cfg["coverage"]
    }
    return p


def test_tickers_converge_to_subject_named_in_title():
    """extract 会把整条产业链都列上(实测一条挂 37 个), 导致同一件事的两篇
    报道 ticker 交集被稀释、聚类合不上, 于是重复推两条快报"""
    it = _mk_item("i", datetime.now(timezone.utc).isoformat())
    it.title = "DeepSeek V4 Pro 正式版上线，多项指标逼近头部模型"
    it.tickers = ["天数智芯", "科大讯飞", "中科曙光", "DeepSeek", "寒武纪", "英伟达"]
    _processor()._validate_item(it, "extract")
    assert it.tickers == ["DeepSeek"]


def test_tickers_keep_all_subjects_named_in_title():
    it = _mk_item("i", datetime.now(timezone.utc).isoformat())
    it.title = "英伟达与台积电扩大CoWoS合作"
    it.tickers = ["英伟达", "台积电", "博通", "美光", "AMD"]
    _processor()._validate_item(it, "extract")
    assert it.tickers == ["英伟达", "台积电"]


def test_tickers_under_cap_untouched():
    it = _mk_item("i", datetime.now(timezone.utc).isoformat())
    it.title = "行业综述"
    it.tickers = ["英伟达", "台积电"]
    _processor()._validate_item(it, "extract")
    assert it.tickers == ["英伟达", "台积电"]


def test_tickers_fall_back_to_direction_when_title_names_none():
    """宏观综述类标题不点名, 退回按多空判断排序"""
    it = _mk_item("i", datetime.now(timezone.utc).isoformat())
    it.title = "美股三大指数收盘涨跌不一"
    it.tickers = ["英伟达", "台积电", "博通", "美光", "AMD"]
    it.direction = {"美光": "positive", "AMD": "negative"}
    _processor()._validate_item(it, "extract")
    assert len(it.tickers) == 4
    assert "美光" in it.tickers and "AMD" in it.tickers


# ================================================================
# 聚合帖源豁免
# ================================================================

def test_import_ai_exempt_from_structural_digest_rule():
    """Import AI 也用分号列话题, 但内容价值远高于氪星晚报"""
    from radar.textnorm import is_digest_title
    title = "Import AI 468: 23 RSI ideas; PostTrainBench; why AI safety is hard"
    assert is_digest_title(title, patterns=[])
    assert not is_digest_title(title, patterns=[], source="rss:importai",
                               exempt_sources=["rss:importai"])


def test_exemption_does_not_cover_keyword_rule():
    """豁免只放过结构判据, 命中栏目名仍然拦"""
    from radar.textnorm import is_digest_title
    assert is_digest_title("Import AI 晚报特辑", patterns=["晚报"],
                           source="rss:importai", exempt_sources=["rss:importai"])


def test_other_sources_not_exempt():
    from radar.textnorm import is_digest_title
    assert is_digest_title(
        "英特尔增发200亿美元；期货市场成交额同比增长38%；汽车出口延续强劲",
        patterns=[], source="rss:36kr", exempt_sources=["rss:importai"])
