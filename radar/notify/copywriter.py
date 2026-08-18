"""LLM 撰稿层 —— 素材 → DigestPayload(结构化 JSON)

失败矩阵(审计 H3), 任一失败统一降级为兜底模板稿, 兜底稿走同一渲染器:
- asyncio.wait_for 单调用超时 —— 先关推理重试一次, 再超时才降级
- LLM 返回空输出
- JSON 解析失败(chat_json 抛 ValueError)
- schema 校验不合规(validate() 抛 ValueError)
- 网络/重试耗尽(RuntimeError)
- API key 缺失(请求时才暴露, minimax_client 仅 warn)

降级不是一次失败就认输: 2026-08-16/17 连续两期内参因单次超时而只剩事实层。
撰稿层给两次机会(见 write_digest), notify.run 还会在当日再排一轮补发。

喂给 LLM 的素材经 assemble.llm_material 剥掉 URL: 看不到链接就编不出链接,
证据一律通过 ref 引用, 由渲染层还原为可点击链接。
"""

import asyncio
import json
import logging
import re
from typing import Optional

from radar.minimax_client import MinimaxClient
from radar.prompts import load_prompt
from radar.notify.assemble import llm_material, material_refs
from radar.textnorm import clip_sentence
from radar.notify.types import (
    DigestPayload, DigestSection, DigestItem, DigestCall, MacroFrame,
    KIND_MORNING, KIND_WEEKLY, KIND_BREAKING,
)

logger = logging.getLogger(__name__)

# 内参撰稿保留模型推理(判断链的"机理"段是报告立身之本, 值得等),
# 因此超时给得比批量任务宽。
#
# 晨报给 300s 而非过去的 210s: 线上典型耗时 98s, 但 M3 的推理尾延迟能冲到 2 倍以上,
# 210s 在 2026-08-16/17 连续两天被冲破, 各吞掉一整期判断链。整个 job 只跑 ~4min,
# job timeout 是 120min, cron 实际间隔 ~25min —— 预算根本不紧张, 紧的是这个常量。
_LLM_TIMEOUT_BY_KIND = {
    KIND_MORNING: 300,
    KIND_WEEKLY: 210,
    KIND_BREAKING: 210,   # 快报是时效产品, 等不起
}

# 首次尝试超时后的第二次机会: 关推理再抢一稿, 预算收紧。
_RETRY_TIMEOUT_SEC = 120

# httpx 必须比外层 wait_for 晚断, 否则外层超时形同虚设(此前 120s < 150s 就是这个问题)。
# 走 chat_json(timeout=) 单请求覆盖, 不动全局 REQUEST_TIMEOUT —— 那是批量各阶段的闸门。
_HTTP_TIMEOUT_MARGIN_SEC = 30

_PROMPT_BY_KIND = {
    KIND_MORNING: "notify_daily",
    KIND_WEEKLY: "notify_weekly",
    KIND_BREAKING: "notify_breaking",
}

_MAX_TOKENS_BY_KIND = {
    KIND_MORNING: 6144,   # 四段式判断链比过去的散文稿长得多
    KIND_WEEKLY: 4096,
    KIND_BREAKING: 2048,  # 1024 会截断长输出导致 JSON 解析失败(06e2098)
}

# 走判断链形态的稿件种类
_CALL_KINDS = {KIND_MORNING}


async def write_digest(
    kind: str,
    material: dict,
    cfg: dict,
    client: MinimaxClient,
    current_time_hkt: str = "",
) -> DigestPayload:
    """LLM 撰稿, 失败时返回兜底模板稿(fallback=True)"""
    prompt_name = _PROMPT_BY_KIND[kind]
    try:
        template = load_prompt(prompt_name)
    except FileNotFoundError as e:
        logger.error(f"Copywriter prompt missing: {e}")
        return fallback_payload(kind, material, current_time_hkt)

    # 所有 kind 一律剥 URL —— 快报素材同样带链接, 不能只堵日报那条通道
    prompt = template.replace(
        "{material_json}", json.dumps(llm_material(material), ensure_ascii=False)
    )
    prompt = prompt.replace("{current_time}", current_time_hkt)
    messages = [{"role": "user", "content": prompt}]

    # 两次机会: 先按论证质量跑(开推理), 超时了再关推理抢一稿。
    # 关推理的稿子机理段偏薄, 但仍是完整的四段式判断链 —— 比只剩事实层的降级稿
    # 好一个数量级, 值得用一次更短的调用去换。只对超时换打法: JSON / schema 不合规
    # 是内容问题, 换个推理开关也治不好, 白白再等一轮。
    attempts = (
        (_LLM_TIMEOUT_BY_KIND[kind], True, "reasoning"),
        (_RETRY_TIMEOUT_SEC, False, "no-reasoning"),
    )
    for i, (budget, thinking, label) in enumerate(attempts, 1):
        try:
            payload = await _attempt(
                kind, material, cfg, client, messages, current_time_hkt,
                budget=budget, thinking=thinking,
            )
            logger.info(
                f"Copywriter [{kind}] ok via attempt {i} ({label}): "
                f"{len(payload.calls)} calls, {len(payload.reviews)} reviews, "
                f"{len(payload.sections)} sections, "
                f"alert={'y' if payload.alert else 'n'}, "
                f"headline {len(payload.headline)} chars"
            )
            return payload
        except asyncio.TimeoutError:
            logger.error(
                f"Copywriter [{kind}] attempt {i} ({label}) timed out after {budget}s"
            )
            continue
        except (ValueError, RuntimeError) as e:
            logger.error(f"Copywriter [{kind}] failed: {e}")
            break
        except Exception as e:
            logger.error(f"Copywriter [{kind}] unexpected error: {e}")
            break
    return fallback_payload(kind, material, current_time_hkt)


async def _attempt(
    kind: str,
    material: dict,
    cfg: dict,
    client: MinimaxClient,
    messages: list[dict],
    current_time_hkt: str,
    *,
    budget: int,
    thinking: bool,
) -> DigestPayload:
    """一次撰稿尝试: 调用 → 解析 → 剪枝 → 校验。任一环节不过就抛。"""
    expect_calls = kind in _CALL_KINDS
    expect_alert = kind == KIND_BREAKING
    valid_refs = material_refs(material)
    expect_reviews = len(((material.get("last_report") or {}).get("calls")) or [])

    raw = await asyncio.wait_for(
        client.chat_json(
            messages,
            model=cfg["minimax"]["model"],
            temperature=0.4,
            max_tokens=_MAX_TOKENS_BY_KIND[kind],
            retries=1,  # JSON 修正只给一次机会, 控制总耗时
            thinking=thinking,
            timeout=budget + _HTTP_TIMEOUT_MARGIN_SEC,
        ),
        timeout=budget,
    )
    if not raw:
        raise ValueError("LLM returned empty output")
    payload = DigestPayload.from_dict(raw, kind=kind)
    dropped = payload.prune_refs(valid_refs)
    if dropped:
        logger.warning(f"Copywriter [{kind}] dropped {dropped} unknown evidence refs")
    payload.validate(expect_calls=expect_calls, expect_reviews=expect_reviews,
                     expect_alert=expect_alert)
    if expect_calls:
        _warn_unquantified_macro(payload)
    if expect_alert:
        _enforce_alert_budget(payload)
        # 时间是系统事实, 不该问 LLM 要。prompt 模板里的 title 是
        # "首席快报"(不含时间), 渲染层从 title 里 split 时间就永远是空的。
        payload.title = f"首席快报 | {current_time_hkt}".rstrip(" |")
    payload.generated_at = material.get("generated_at", "")
    return payload


# 快报字数上限。目标 100-150 字, 超过 200 就不叫"快报"了。
# prompt 里写了约束但 LLM 会无视, 所以在代码里兜底 —— 按句子边界裁, 不切半句。
_ALERT_FIELD_BUDGET = {"summary": 90, "why": 80, "watch": 80}
_ALERT_TOTAL_WARN = 200


_DIGIT_RE = re.compile(r"\d")


def _warn_unquantified_macro(payload: DigestPayload) -> None:
    """周期位置不带数字就是印象派描述, 但不能因此判稿件不合格

    校验失败会掉进降级稿(只有事实层, 没有机理与推论), 代价远大于一句
    缺数字的周期判断。与 _enforce_alert_budget 同一处理哲学: 记账不拦路。
    """
    cycle = payload.macro.cycle if payload.macro else ""
    if cycle and not _DIGIT_RE.search(cycle):
        logger.warning(f"Macro cycle has no quantitative anchor: {cycle[:60]}")


def _enforce_alert_budget(payload: DigestPayload) -> None:
    alert = payload.alert
    if alert is None:
        return
    for field_name, budget in _ALERT_FIELD_BUDGET.items():
        text = getattr(alert, field_name, "")
        if len(text) > budget:
            # hard=False: 只丢弃末尾整句, 绝不切出半句。线上出现过
            # "…谁有自研芯片谁就能留住…" 这种被砍断的判断, 比略长更糟。
            setattr(alert, field_name, clip_sentence(text, budget, hard=False))
    total = len(alert.summary) + len(alert.why) + len(alert.watch)
    if total > _ALERT_TOTAL_WARN:
        logger.warning(f"Alert over budget: {total} chars (target 100-150)")


# 态势原文常以「其一…；其二…」列举, 按长度截断会砍掉后半, 而前半还写着"两条"。
# 说了几条就得给几条 —— 配对项没跟上就整段弃用, 宁可平淡, 不可食言。
_ENUM_PAIRS = (("其一", "其二"), ("一是", "二是"), ("首先", "其次"))

_CJK_RE = re.compile(r"[一-鿿]")


# clip_sentence 把「；」当句末边界, 于是列举句会正好停在分号上 —— 而那个分号
# 恰恰是"后面还有"的信号。换成省略号, 至少不假装话已经说完。
_TRAILING_SEPARATORS = "；;、，,"


def _has_dangling_enum(text: str) -> bool:
    return any(lead in text and follow not in text for lead, follow in _ENUM_PAIRS)


def _tidy_tail(text: str) -> str:
    text = text.rstrip()
    if text and text[-1] in _TRAILING_SEPARATORS:
        return text[:-1].rstrip() + "…"
    return text


def _fallback_headline(material: dict) -> str:
    """降级稿导语: 用得上态势摘录就用, 用不上就据实说明本期是什么"""
    situation = (material.get("situation") or "").strip()
    if situation:
        # 态势原文可达数百字, 直接当导语会把报头压垮; 按句子边界截到导语长度
        clipped = _tidy_tail(clip_sentence(situation, 120))
        if clipped and not _has_dangling_enum(clipped):
            return clipped
    hours = int(material.get("window_hours") or 24)
    n = len(material.get("events") or [])
    return f"本期撰稿环节未完成，以下为 {hours}h 窗口内 {n} 个事件的事实层罗列。"


def _claim_text(ev: dict) -> str:
    """判断句优先说中文

    外文源的 title 是原文未译, 而 summary 已经是中文。过去直接拿 title 当判断句,
    于是「判断二 · Nvidia dramatically reduces...」配一段中文正文。英文原题在证据行里
    本来就完整展示, 这里换成中文摘要首句不丢任何信息。
    """
    title = (ev.get("title") or "").strip()
    summary = (ev.get("summary") or "").strip()
    if _CJK_RE.search(title) or not _CJK_RE.search(summary):
        return title
    return clip_sentence(summary, 40)


def _direction_text(tickers: list, budget: int = 40) -> str:
    """按 ticker 边界拼接

    ", ".join(...)[:40] 会切出半个标的名。与 _enforce_alert_budget 同一条原则:
    宁可少给一个标的, 不给一个残缺的标的名。
    """
    out: list[str] = []
    used = 0
    for tk in tickers or []:
        tk = str(tk).strip()
        if not tk:
            continue
        cost = len(tk) + (2 if out else 0)
        if used + cost > budget:
            break
        out.append(tk)
        used += cost
    return ", ".join(out)


def fallback_payload(kind: str, material: dict, current_time_hkt: str = "") -> DigestPayload:
    """兜底模板稿 —— 不用 LLM, 直接从素材拼装, 保证版式一致

    判断链降级为"只有事实层"的条目: 有事实与证据, 没有机理与推论。
    报头会标注降级, 让读者知道这期不是完整撰稿, 而不是以为首席今天没话说。
    """
    title_prefix = {
        KIND_MORNING: "AI 首席内参",
        KIND_WEEKLY: "Sterling 周末复盘",
        KIND_BREAKING: "首席快报",
    }[kind]

    payload = DigestPayload(
        kind=kind,
        title=f"{title_prefix} | {current_time_hkt}".rstrip(" |"),
        headline=_fallback_headline(material),
        generated_at=material.get("generated_at", ""),
        fallback=True,
    )

    if kind == KIND_BREAKING:
        # 降级快报: 只有事实层, 没有含义与跟踪点
        ev = material.get("event") or {}
        payload.headline = ""
        title = ev.get("title", "")
        summary = ev.get("summary", "")
        payload.alert = DigestItem(
            title=title,
            # 标题带着事件主体, 降级稿不能只留摘要 —— 否则读者看到的是没有主语的半句
            summary=f"{title}。{summary}" if title and summary else (title or summary),
            tickers=(ev.get("tickers") or [])[:3],
            evidence_ref=ev.get("ref", ""),
        )
        return payload

    events = (material.get("events") or [])[:8]

    if kind == KIND_MORNING:
        # 降级判断链: 每个高重要性事件出一条只有事实层的 call
        for i, ev in enumerate(events[:4], 1):
            payload.calls.append(DigestCall(
                n=i,
                claim=_claim_text(ev),
                fact=ev.get("summary", "") or ev.get("title", ""),
                evidence_refs=[ev["ref"]] if ev.get("ref") else [],
                direction=_direction_text(ev.get("tickers")),
                counterpoint=ev.get("counterpoint", ""),
            ))
        payload.macro = MacroFrame(
            shift_kind="hold", shift="本期撰稿降级, 未更新框架",
        )
        payload.watchlist = [
            {"text": ev.get("title", ""), "ref": ev.get("ref", "")}
            for ev in events[4:8] if ev.get("title")
        ]
        return payload

    # weekly: 事件平铺为叙述段落(不用条目块, 保持散文形态)
    paragraphs = []
    for ev in events:
        text = ev.get("title", "")
        if ev.get("summary"):
            text += f"。{ev['summary']}"
        paragraphs.append(text)
    if paragraphs:
        payload.sections = [DigestSection(heading="要闻回顾", paragraphs=paragraphs)]
    return payload
