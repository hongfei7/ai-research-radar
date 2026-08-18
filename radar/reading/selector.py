"""阅读筛选 —— 对当轮采集到的条目跑「值不值得读」rubric

与 processor.triage 是两把不同的尺子, 共用的只有批处理的写法:
分批 → 一次 chat_json → 映射回条目 → 过闸。单批失败只丢这一批, 不影响其余。
"""

import json
import logging

from radar.minimax_client import MinimaxClient
from radar.models import Item
from radar.prompts import load_prompt

logger = logging.getLogger(__name__)

_BATCH_SIZE = 40
# 摘要在喂给 rubric 前截断: 判断"值不值得读"用不到全文, 全文留给入选后的笔记环节
_SUMMARY_CLIP = 600

VALID_ANGLES = ("research", "route", "industry", "engineering")
_ANGLE_LABEL = {
    "research": "一手研究解读",
    "route": "技术路线之争",
    "industry": "产业与商业判断",
    "engineering": "工程实践与复现",
}


def angle_label(angle: str) -> str:
    return _ANGLE_LABEL.get(angle, "其他")


async def select(items: list[Item], cfg: dict, client: MinimaxClient) -> list[dict]:
    """返回过闸的候选 [{id,title,url,source,published_at,score,angle,why,...}]"""
    if not items:
        return []

    r_cfg = cfg.get("reading", {})
    min_score = r_cfg.get("min_score", 7)

    try:
        template = load_prompt("reading_triage")
    except FileNotFoundError as e:
        logger.error(f"Reading triage prompt missing: {e}")
        return []

    by_id = {it.id: it for it in items}
    scored: list[dict] = []

    for i in range(0, len(items), _BATCH_SIZE):
        batch = items[i : i + _BATCH_SIZE]
        batch_json = json.dumps(
            [
                {
                    "id": it.id,
                    "title": it.title,
                    "source": it.source,
                    "summary": (it.raw_summary or "")[:_SUMMARY_CLIP],
                }
                for it in batch
            ],
            ensure_ascii=False,
        )
        prompt = template.replace("{items_json}", batch_json)
        try:
            result = await client.chat_json(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=4096,
                thinking=False,  # 批量打分, 推理带来的延迟远大于收益
            )
        except Exception as e:
            logger.error(f"Reading triage batch {i // _BATCH_SIZE + 1} failed: {e}")
            continue
        if isinstance(result, list):
            scored.extend(x for x in result if isinstance(x, dict))
        elif isinstance(result, dict):
            scored.append(result)

    candidates: list[dict] = []
    for s in scored:
        item = by_id.get(s.get("id"))
        if item is None:
            continue
        try:
            score = int(s.get("score", 0))
        except (TypeError, ValueError):
            logger.warning(f"Reading triage: non-numeric score for {s.get('id')}")
            continue
        if score < min_score:
            continue
        angle = str(s.get("angle", "")).strip().lower()
        if angle not in VALID_ANGLES:
            # 归一化而非丢弃: 分数已经过闸, 不该因为一个标签写歪就整条丢掉
            angle = "industry"
        candidates.append({
            "id": item.id,
            "title": item.title,
            "url": item.url,
            "source": item.source,
            "published_at": item.published_at,
            "raw_summary": item.raw_summary or "",
            "score": score,
            "angle": angle,
            "source_traceable": bool(s.get("source_traceable", False)),
            "why": str(s.get("why", "")).strip(),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    logger.info(
        f"Reading select: {len(items)} items → {len(scored)} scored → "
        f"{len(candidates)} pass (min_score={min_score})"
    )
    return candidates
