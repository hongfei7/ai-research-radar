"""深度解读阅读流单元测试

覆盖: rubric 解析容错 / 队列滚动与配额 / 出刊时点 / 全文抓取降级 / 清单渲染
"""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from radar.models import Item
from radar.reading import fulltext, render
from radar.reading.selector import select, angle_label, VALID_ANGLES
from radar.reading.state import enqueue, prune_stale
from radar.reading.run import is_due

HKT = ZoneInfo("Asia/Hong_Kong")


def _cfg(**over):
    r = {
        "enabled": True, "hour_hkt": 9,
        "window_before_min": 15, "window_after_min": 90,
        "min_score": 7, "daily_limit": 5, "queue_cap": 40, "fulltext": True,
    }
    r.update(over)
    return {"reading": r}


def _item(iid: str, title: str = "标题", summary: str = "摘要") -> Item:
    return Item(
        id=iid, title=title, url=f"https://example.com/{iid}",
        source="rss:qbitai", source_type="tech",
        published_at="2026-08-17T00:00:00Z", fetched_at="2026-08-17T00:00:00Z",
        raw_summary=summary,
    )


class _Client:
    """按脚本返回 rubric 结果"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def chat_json(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.payload


# ================================================================
# rubric 筛选
# ================================================================

def test_select_keeps_only_above_threshold():
    items = [_item("a"), _item("b")]
    client = _Client([
        {"id": "a", "score": 9, "angle": "research", "why": "有一手实验"},
        {"id": "b", "score": 4, "angle": "industry", "why": "转述"},
    ])
    out = asyncio.run(select(items, _cfg(), client))
    assert [c["id"] for c in out] == ["a"]


def test_select_uses_no_reasoning_for_batch_scoring():
    """批量打分不开推理 —— 延迟远大于收益"""
    client = _Client([])
    asyncio.run(select([_item("a")], _cfg(), client))
    assert client.calls[0]["thinking"] is False


def test_select_normalizes_bad_angle():
    """分数已经过闸, 不该因为一个标签写歪就整条丢掉"""
    client = _Client([{"id": "a", "score": 8, "angle": "无关分类", "why": "x"}])
    out = asyncio.run(select([_item("a")], _cfg(), client))
    assert len(out) == 1
    assert out[0]["angle"] in VALID_ANGLES


def test_select_skips_unparseable_rows():
    """缺 id / 非数字 score 静默跳过, 不能让一行脏数据毁掉整批"""
    client = _Client([
        {"score": 9, "angle": "research"},                    # 缺 id
        {"id": "a", "score": "很高", "angle": "research"},     # score 非数字
        {"id": "b", "score": 8, "angle": "route", "why": "ok"},
    ])
    out = asyncio.run(select([_item("a"), _item("b")], _cfg(), client))
    assert [c["id"] for c in out] == ["b"]


def test_select_survives_llm_exception():
    class _Boom:
        async def chat_json(self, messages, **kwargs):
            raise RuntimeError("API down")

    out = asyncio.run(select([_item("a")], _cfg(), _Boom()))
    assert out == []


def test_select_sorts_by_score_desc():
    client = _Client([
        {"id": "a", "score": 7, "angle": "route", "why": ""},
        {"id": "b", "score": 10, "angle": "research", "why": ""},
    ])
    out = asyncio.run(select([_item("a"), _item("b")], _cfg(), client))
    assert [c["score"] for c in out] == [10, 7]


# ================================================================
# 候选队列
# ================================================================

def test_enqueue_dedups_same_article():
    """同一篇可能被多个信源采到, 不该在清单里出现两次"""
    state = {"candidates": []}
    cand = [{"id": "a", "score": 8}]
    assert enqueue(state, cand, cap=40) == 1
    assert enqueue(state, cand, cap=40) == 0
    assert len(state["candidates"]) == 1


def test_enqueue_survives_across_midnight():
    """队列由出刊消费, 不按日历切 —— 09:00 之后采到的要留到下一份清单

    这是最初的设计缺陷: 按自然日清零会让每天 09:00 之后的采集全部白扔。
    """
    state = {"candidates": []}
    enqueue(state, [{"id": "evening", "score": 9}],
            cap=40, now=datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc))
    enqueue(state, [{"id": "morning", "score": 8}],
            cap=40, now=datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc))
    assert {c["id"] for c in state["candidates"]} == {"evening", "morning"}


def test_enqueue_prunes_stale_candidates():
    """出刊连续失败时队列不能无限堆积陈货"""
    state = {"candidates": []}
    enqueue(state, [{"id": "old", "score": 9}],
            cap=40, now=datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc))
    enqueue(state, [{"id": "fresh", "score": 8}],
            cap=40, now=datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc))
    assert [c["id"] for c in state["candidates"]] == ["fresh"]


def test_prune_keeps_entries_without_timestamp():
    """没有可解析入队时间的条目宁可多留一轮, 不误杀"""
    state = {"candidates": [{"id": "a", "score": 9, "queued_at": ""}]}
    assert prune_stale(state) == 0
    assert len(state["candidates"]) == 1


def test_enqueue_cap_keeps_highest_scores():
    state = {"candidates": []}
    cands = [{"id": str(i), "score": i} for i in range(10)]
    enqueue(state, cands, cap=3)
    assert sorted(c["score"] for c in state["candidates"]) == [7, 8, 9]


# ================================================================
# 出刊时点
# ================================================================

def test_due_inside_window():
    now = datetime(2026, 8, 17, 9, 0, tzinfo=HKT)
    assert is_due(_cfg(), {"digest_last_date": ""}, now=now)


def test_due_only_once_per_day():
    now = datetime(2026, 8, 17, 9, 0, tzinfo=HKT)
    assert not is_due(_cfg(), {"digest_last_date": "2026-08-17"}, now=now)


def test_due_late_catchup():
    """过窗补发, 容忍 Actions 排队抖动"""
    now = datetime(2026, 8, 17, 14, 0, tzinfo=HKT)
    assert is_due(_cfg(), {"digest_last_date": ""}, now=now)


def test_not_due_late_at_night():
    """凌晨三点出一份"今日值得读"没有意义"""
    now = datetime(2026, 8, 17, 23, 30, tzinfo=HKT)
    assert not is_due(_cfg(), {"digest_last_date": ""}, now=now)


def test_not_due_before_window():
    now = datetime(2026, 8, 17, 6, 0, tzinfo=HKT)
    assert not is_due(_cfg(), {"digest_last_date": ""}, now=now)


def test_not_due_when_disabled():
    now = datetime(2026, 8, 17, 9, 0, tzinfo=HKT)
    assert not is_due(_cfg(enabled=False), {"digest_last_date": ""}, now=now)


# ================================================================
# 全文抓取
# ================================================================

def test_extract_text_drops_navigation_noise():
    html = """
    <html><body>
      <nav>首页 关于 订阅</nav>
      <script>var x = 1;</script>
      <article><p>这是正文第一段。</p><p>这是正文第二段。</p></article>
      <footer>版权所有</footer>
    </body></html>
    """
    text = fulltext.extract_text(html)
    assert "正文第一段" in text and "正文第二段" in text
    assert "订阅" not in text and "版权所有" not in text and "var x" not in text


def test_extract_text_handles_garbage():
    assert fulltext.extract_text("") == ""
    assert fulltext.extract_text("not html at all") is not None


def test_fetch_one_returns_empty_on_bad_url():
    """抓不到就退回 raw_summary, 不抛异常"""
    assert asyncio.run(fulltext.fetch_one("")) == ""


# ================================================================
# 渲染
# ================================================================

def _picked(n=1, with_note=True):
    out = []
    for i in range(n):
        c = {
            "id": str(i), "title": f"文章{i}", "url": f"https://example.com/{i}",
            "source": "rss:qbitai", "published_at": "2026-08-17T00:00:00Z",
            "score": 9 - i, "angle": "research", "source_traceable": True,
            "why": f"理由{i}",
        }
        if with_note:
            c["note"] = {"claim": f"主张{i}", "evidence": f"证据{i}",
                         "gap": f"薄弱{i}", "tension": f"冲突{i}",
                         "followup": f"延伸{i}"}
        out.append(c)
    return out


def test_render_includes_note_sections_and_link():
    body = render.render_digest(_picked(1), [], "2026-08-17")
    assert "[文章0](https://example.com/0)" in body
    for label in ("主张", "证据", "薄弱处", "冲突点", "延伸"):
        assert f"**{label}**" in body
    assert angle_label("research") in body


def test_render_falls_back_when_note_missing():
    """笔记失败也要交代筛选理由, 不能留一个空标题"""
    body = render.render_digest(_picked(1, with_note=False), [], "2026-08-17")
    assert "入选理由" in body and "理由0" in body
    assert "笔记生成失败" in body


def test_render_lists_skipped_items():
    body = render.render_digest(_picked(1), _picked(2), "2026-08-17")
    assert "## 未入选" in body


def test_render_empty_day_says_so():
    """宁可空着, 不凑数"""
    body = render.render_digest([], [], "2026-08-17")
    assert "没有条目通过筛选" in body


def test_render_report_file_frontmatter():
    content = render.render_report_file("# body", "2026-08-17", picked=3,
                                        issue_url="https://x/1")
    assert content.startswith("---\n")
    assert "date: 2026-08-17" in content
    assert "kind: reading" in content
    assert "picked: 3" in content


def test_digest_body_carries_no_invented_links():
    """正文里的链接只能来自候选记录, 不能出现别的域名"""
    body = render.render_digest(_picked(2), [], "2026-08-17")
    import re
    urls = re.findall(r"\]\((https?://[^)]+)\)", body)
    assert urls
    assert all(u.startswith("https://example.com/") for u in urls)


# ================================================================
# 每日配额
# ================================================================

def test_daily_digest_caps_at_limit(monkeypatch):
    """队列 12 条 → 清单恰好 5 条, 其余落到未入选一节"""
    from radar.models import today_str
    from radar.reading import run as run_mod

    queue = [{"id": str(i), "title": f"文章{i}", "url": f"https://example.com/{i}",
              "source": "rss:qbitai", "published_at": "2026-08-17T00:00:00Z",
              "score": i, "angle": "research", "why": f"理由{i}",
              "queued_at": "2026-08-17T00:00:00Z"}
             for i in range(12)]

    monkeypatch.setattr(run_mod, "load_reading_state",
                        lambda: {"candidates": queue, "digest_last_date": ""})
    monkeypatch.setattr(run_mod, "prune_stale", lambda *a, **kw: 0)
    # 出稿路径不该触网: 全文抓取与笔记生成都打桩
    async def _no_fulltext(c):
        return 0
    async def _no_notes(c, client):
        return 0
    monkeypatch.setattr(run_mod.fulltext, "enrich", _no_fulltext)
    monkeypatch.setattr(run_mod.notes, "write_notes", _no_notes)

    body = asyncio.run(run_mod.run_daily(_cfg(), client=None,
                                         dry_run=True, force=True))

    # 分数最高的 5 篇入选(11..7), 其余 7 篇列在"未入选"
    assert body.count("\n## ") == 6          # 5 篇正文 + 1 个"未入选"标题
    assert "## 未入选" in body
    for i in (11, 10, 9, 8, 7):
        assert f"## " in body and f"文章{i}" in body
    assert "*5 篇入选*" in body


def test_daily_digest_drops_stale_queue_entries(monkeypatch):
    """久未出刊时队列里的陈货不该冒充"今日值得读" """
    from radar.reading import run as run_mod

    monkeypatch.setattr(run_mod, "load_reading_state",
                        lambda: {"candidates": [
                            {"id": "old", "title": "旧文", "score": 10,
                             "queued_at": "2000-01-01T00:00:00Z"}],
                            "digest_last_date": ""})
    body = asyncio.run(run_mod.run_daily(_cfg(), client=None,
                                         dry_run=True, force=True))
    assert "没有条目通过筛选" in body
    assert "旧文" not in body


def test_daily_digest_clears_queue_after_publishing(monkeypatch, tmp_path):
    """出刊即清空: 未入选的已留档, 不该再跟明天的新货竞争"""
    from radar.reading import run as run_mod

    state = {"candidates": [
        {"id": str(i), "title": f"文章{i}", "url": f"https://example.com/{i}",
         "score": i, "angle": "research", "why": "",
         "queued_at": "2026-08-17T00:00:00Z"} for i in range(8)],
        "digest_last_date": ""}
    saved = {}

    async def _no_notes(c, client):
        return 0
    async def _no_issue(*a, **kw):
        return ""

    monkeypatch.setattr(run_mod, "load_reading_state", lambda: state)
    monkeypatch.setattr(run_mod, "save_reading_state", lambda s: saved.update(s))
    monkeypatch.setattr(run_mod, "prune_stale", lambda *a, **kw: 0)
    monkeypatch.setattr(run_mod.notes, "write_notes", _no_notes)
    monkeypatch.setattr(run_mod, "_archive_issue", _no_issue)
    monkeypatch.setattr(run_mod, "_write_report_file", lambda *a, **kw: None)
    monkeypatch.setattr(run_mod, "_REPORTS_DIR", tmp_path)

    cfg = _cfg(fulltext=False)
    asyncio.run(run_mod.run_daily(cfg, client=None, dry_run=False, force=True))

    assert saved["candidates"] == []
    assert saved["digest_last_date"]


def test_extract_text_drops_literal_tag_lines():
    """实测量子位会把转义后的 <img> 当正文文本吐出来, 不能混进笔记素材"""
    html = ('<article><p>&lt; img id="wx_img" src="https://x/a.png" width="400"&gt;</p>'
            '<p>正文开始。</p></article>')
    text = fulltext.extract_text(html)
    assert "wx_img" not in text
    assert "正文开始。" in text


def test_collect_candidates_writes_nothing_in_dry_run(monkeypatch):
    """dry-run 不该改动队列状态"""
    from radar.reading import run as run_mod

    monkeypatch.setattr(run_mod, "save_reading_state",
                        lambda s: (_ for _ in ()).throw(
                            AssertionError("dry-run 不该写状态")))
    client = _Client([{"id": "a", "score": 9, "angle": "research", "why": "x"}])
    added = asyncio.run(run_mod.collect_candidates(
        [_item("a")], _cfg(), client, dry_run=True))
    assert added == 0
