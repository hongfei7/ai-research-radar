"""LLM 撰稿层 —— 素材 → DigestPayload(结构化 JSON)

失败矩阵(审计 H3), 任一失败统一降级为兜底模板稿, 兜底稿走同一渲染器:
- asyncio.wait_for 单调用 ≤90s 超时
- LLM 返回空输出
- JSON 解析失败(chat_json 抛 ValueError)
- schema 校验不合规(validate() 抛 ValueError)
- 网络/重试耗尽(RuntimeError)
- API key 缺失(请求时才暴露, minimax_client 仅 warn)
"""

import asyncio
import json
import logging
from typing import Optional

from radar.minimax_client import MinimaxClient
from radar.prompts import load_prompt
from radar.notify.types import (
    DigestPayload, DigestSection, DigestItem,
    KIND_MORNING, KIND_DIGEST, KIND_BREAKING,
)

logger = logging.getLogger(__name__)

_LLM_TIMEOUT_SEC = 90

_PROMPT_BY_KIND = {
    KIND_MORNING: "notify_morning",
    KIND_DIGEST: "notify_digest",
    KIND_BREAKING: "notify_breaking",
}

_MAX_TOKENS_BY_KIND = {
    KIND_MORNING: 4096,
    KIND_DIGEST: 2048,
    KIND_BREAKING: 1024,
}


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

    prompt = template.replace("{material_json}", json.dumps(material, ensure_ascii=False))
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
            ),
            timeout=_LLM_TIMEOUT_SEC,
        )
        if not raw:
            raise ValueError("LLM returned empty output")
        payload = DigestPayload.from_dict(raw, kind=kind)
        payload.validate()
        logger.info(
            f"Copywriter [{kind}] ok: {len(payload.sections)} sections, "
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


def fallback_payload(kind: str, material: dict, current_time_hkt: str = "") -> DigestPayload:
    """兜底模板稿 —— 不用 LLM, 直接从素材拼装, 保证版式一致"""
    title_prefix = {
        KIND_MORNING: "AI 投研雷达 · 晨报",
        KIND_DIGEST: "AI 投研雷达 · 速递",
        KIND_BREAKING: "AI 投研雷达 · 快讯",
    }[kind]

    payload = DigestPayload(
        kind=kind,
        title=f"{title_prefix} · {current_time_hkt}".rstrip(" ·"),
        headline=(material.get("situation") or "").strip(),
        generated_at=material.get("generated_at", ""),
        fallback=True,
    )

    if kind == KIND_BREAKING:
        ev = material.get("event") or {}
        d = ev.get("direction") or {}
        first_dir = next(iter(d.values()), "")
        payload.headline = ""
        payload.sections = [DigestSection(
            heading="",
            items=[DigestItem(
                title=ev.get("title", ""),
                tickers=ev.get("tickers", []),
                direction=first_dir,
                significance=ev.get("significance", 0),
                summary=ev.get("summary", ""),
                why=ev.get("deep_analysis", ""),
            )],
        )]
        return payload

    # morning / digest: 事件列表按重要性平铺
    items = []
    for ev in (material.get("events") or [])[:10]:
        d = ev.get("direction") or {}
        first_dir = next(iter(d.values()), "")
        items.append(DigestItem(
            title=ev.get("title", ""),
            tickers=ev.get("tickers", []),
            direction=first_dir,
            significance=ev.get("significance", 0),
            summary=ev.get("summary", ""),
        ))
    if items:
        payload.sections = [DigestSection(heading="要闻", items=items)]
    return payload
