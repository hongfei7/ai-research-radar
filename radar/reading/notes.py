"""逐篇生成结构化阅读笔记

喂给 LLM 的素材不含 URL —— 与 notify.assemble.llm_material 同一条纪律:
看不到链接就编不出链接。每篇笔记只对应一个已知的原文链接, 由渲染层填。

单篇失败不影响其余: 拿不到笔记的条目退化为"只有筛选理由"的一行, 仍然出现在清单里。
"""

import asyncio
import json
import logging

from radar.minimax_client import MinimaxClient
from radar.prompts import load_prompt

logger = logging.getLogger(__name__)

_CONCURRENCY = 3
_LLM_TIMEOUT_SEC = 120
_HTTP_TIMEOUT_MARGIN_SEC = 30
# 正文喂给 LLM 的上限。比 fulltext 抓取上限小: 笔记要的是论证骨架, 不是全文复读
_BODY_CLIP = 5000

NOTE_FIELDS = ("claim", "evidence", "gap", "tension", "followup")


def _material(cand: dict) -> dict:
    """构造单篇素材, 剥掉 URL"""
    body = cand.get("fulltext") or cand.get("raw_summary") or ""
    return {
        "title": cand.get("title", ""),
        "source": cand.get("source", ""),
        "published_at": cand.get("published_at", ""),
        "angle": cand.get("angle", ""),
        "body": body[:_BODY_CLIP],
        "body_truncated": len(body) > _BODY_CLIP,
    }


async def write_note(cand: dict, client: MinimaxClient) -> dict:
    """返回 {claim,evidence,gap,tension,followup}, 失败返回空 dict"""
    try:
        template = load_prompt("reading_note")
    except FileNotFoundError as e:
        logger.error(f"Reading note prompt missing: {e}")
        return {}

    prompt = template.replace(
        "{article_json}", json.dumps(_material(cand), ensure_ascii=False)
    )
    try:
        raw = await asyncio.wait_for(
            client.chat_json(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=1536,
                retries=1,
                thinking=True,  # "作者没说的"这一段需要推理, 是笔记里最值钱的部分
                timeout=_LLM_TIMEOUT_SEC + _HTTP_TIMEOUT_MARGIN_SEC,
            ),
            timeout=_LLM_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Reading note timed out: {cand.get('title', '')[:40]}")
        return {}
    except Exception as e:
        logger.warning(f"Reading note failed ({type(e).__name__}): {cand.get('title', '')[:40]}")
        return {}

    if not isinstance(raw, dict):
        return {}
    note = {k: str(raw.get(k, "")).strip() for k in NOTE_FIELDS}
    # 全空说明 LLM 交了个空壳, 当失败处理, 让渲染层走"仅筛选理由"的降级形态
    return note if any(note.values()) else {}


async def write_notes(candidates: list[dict], client: MinimaxClient) -> int:
    """就地给候选补 note 字段, 返回成功篇数"""
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(cand: dict):
        async with sem:
            cand["note"] = await write_note(cand, client)

    await asyncio.gather(*[_one(c) for c in candidates], return_exceptions=True)
    ok = sum(1 for c in candidates if c.get("note"))
    logger.info(f"Reading notes: {ok}/{len(candidates)} written")
    return ok
