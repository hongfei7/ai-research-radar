"""LLM 撰稿层 —— 素材 → DigestPayload(结构化 JSON)

失败矩阵(审计 H3), 任一失败统一降级为兜底模板稿, 兜底稿走同一渲染器:
- asyncio.wait_for 单调用超时
- LLM 返回空输出
- JSON 解析失败(chat_json 抛 ValueError)
- schema 校验不合规(validate() 抛 ValueError)
- 网络/重试耗尽(RuntimeError)
- API key 缺失(请求时才暴露, minimax_client 仅 warn)

喂给 LLM 的素材经 assemble.llm_material 剥掉 URL: 看不到链接就编不出链接,
证据一律通过 ref 引用, 由渲染层还原为可点击链接。
"""

import asyncio
import json
import logging
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
# 因此超时给得比批量任务宽。须小于 minimax_client.REQUEST_TIMEOUT, 否则 httpx 先断。
_LLM_TIMEOUT_SEC = 210

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

    expect_calls = kind in _CALL_KINDS
    expect_alert = kind == KIND_BREAKING
    valid_refs = material_refs(material)
    expect_reviews = len(((material.get("last_report") or {}).get("calls")) or [])

    # 所有 kind 一律剥 URL —— 快报素材同样带链接, 不能只堵日报那条通道
    prompt = template.replace(
        "{material_json}", json.dumps(llm_material(material), ensure_ascii=False)
    )
    prompt = prompt.replace("{current_time}", current_time_hkt)

    messages = [{"role": "user", "content": prompt}]
    try:
        raw = await asyncio.wait_for(
            client.chat_json(
                messages,
                model=cfg["minimax"]["model"],
                temperature=0.4,
                max_tokens=_MAX_TOKENS_BY_KIND[kind],
                retries=1,  # JSON 修正只给一次机会, 控制总耗时
                thinking=True,
            ),
            timeout=_LLM_TIMEOUT_SEC,
        )
        if not raw:
            raise ValueError("LLM returned empty output")
        payload = DigestPayload.from_dict(raw, kind=kind)
        dropped = payload.prune_refs(valid_refs)
        if dropped:
            logger.warning(f"Copywriter [{kind}] dropped {dropped} unknown evidence refs")
        payload.validate(expect_calls=expect_calls, expect_reviews=expect_reviews,
                         expect_alert=expect_alert)
        if expect_alert:
            _enforce_alert_budget(payload)
        payload.generated_at = material.get("generated_at", "")
        logger.info(
            f"Copywriter [{kind}] ok: {len(payload.calls)} calls, "
            f"{len(payload.reviews)} reviews, {len(payload.sections)} sections, "
            f"alert={'y' if payload.alert else 'n'}, "
            f"headline {len(payload.headline)} chars"
        )
        return payload
    except asyncio.TimeoutError:
        logger.error(f"Copywriter [{kind}] timed out after {_LLM_TIMEOUT_SEC}s")
    except (ValueError, RuntimeError) as e:
        logger.error(f"Copywriter [{kind}] failed: {e}")
    except Exception as e:
        logger.error(f"Copywriter [{kind}] unexpected error: {e}")
    return fallback_payload(kind, material, current_time_hkt)


# 快报字数上限。目标 100-150 字, 超过 200 就不叫"快报"了。
# prompt 里写了约束但 LLM 会无视, 所以在代码里兜底 —— 按句子边界裁, 不切半句。
_ALERT_FIELD_BUDGET = {"summary": 110, "why": 80, "watch": 80}
_ALERT_TOTAL_WARN = 200


def _enforce_alert_budget(payload: DigestPayload) -> None:
    alert = payload.alert
    if alert is None:
        return
    for field_name, budget in _ALERT_FIELD_BUDGET.items():
        text = getattr(alert, field_name, "")
        if len(text) > budget:
            setattr(alert, field_name, clip_sentence(text, budget))
    total = len(alert.summary) + len(alert.why) + len(alert.watch)
    if total > _ALERT_TOTAL_WARN:
        logger.warning(f"Alert over budget: {total} chars (target 100-150)")


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
        # 态势原文可达数百字, 直接当导语会把报头压垮; 按句子边界截到导语长度
        headline=clip_sentence((material.get("situation") or "").strip(), 120),
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
                claim=ev.get("title", ""),
                fact=ev.get("summary", "") or ev.get("title", ""),
                evidence_refs=[ev["ref"]] if ev.get("ref") else [],
                direction=", ".join(ev.get("tickers") or [])[:40],
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
