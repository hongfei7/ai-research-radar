"""滚动态势生成 —— 持续重写的"此刻 AI 板块在发生什么" """

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from radar.models import Item, Event, Situation, utcnow_iso
from radar.minimax_client import MinimaxClient

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


class SituationGenerator:
    """滚动态势生成器 —— 每 N 轮或检测到新事件时，让 LLM 重写当前态势"""

    def __init__(self, client: MinimaxClient, cfg: dict):
        self.client = client
        self.cfg = cfg
        self.update_interval = cfg["runtime"].get("situation_update_interval", 3)
        self.trigger_new_events = cfg["runtime"].get("situation_trigger_new_events", 2)

    def should_update(
        self,
        prev: Optional[Situation],
        run_count: int,
        new_event_count: int,
    ) -> bool:
        """
        判断是否需要更新态势。
        - 首次运行 → 需要
        - 距上次 ≥ update_interval 轮 → 需要
        - 本轮新事件 ≥ trigger_new_events → 需要
        """
        if prev is None or not prev.generated_at:
            return True

        if run_count % self.update_interval == 0:
            return True

        if new_event_count >= self.trigger_new_events:
            return True

        # 超过 2 小时未更新也触发
        try:
            last_gen = datetime.fromisoformat(prev.generated_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if (now - last_gen).total_seconds() > 7200:
                return True
        except Exception:
            return True

        return False

    async def generate(
        self,
        events: dict[str, Event],
        recent_items: list[Item],
        prev_situation: Optional[Situation] = None,
    ) -> Situation:
        """
        生成/更新当前态势综述。

        Args:
            events:         当前活跃事件
            recent_items:   本轮新处理的条目
            prev_situation: 上次态势（null = 首次生成）
        """
        template = _load_prompt("situation")

        # 活跃事件摘要
        active_events = {eid: ev for eid, ev in events.items() if ev.is_active}
        active_json = json.dumps(
            [
                {
                    "title": ev.title,
                    "summary": ev.summary,
                    "status": ev.status,
                    "significance": ev.significance,
                }
                for ev in active_events.values()
            ],
            ensure_ascii=False,
        )

        # 最新条目摘要
        recent_text = "\n".join(
            f"- [{it.relevance_score}分] {it.title}: {it.cn_summary}"
            for it in recent_items[:20]
        )

        prev_text = ""
        if prev_situation and prev_situation.text:
            prev_text = prev_situation.text

        # 当前时间（HKT）
        try:
            from zoneinfo import ZoneInfo
            hkt = ZoneInfo("Asia/Hong_Kong")
        except Exception:
            hkt = timezone.utc
        current_time = datetime.now(hkt).strftime("%Y-%m-%d %H:%M HKT")

        prompt = template.format(
            active_events_json=active_json,
            recent_items_text=recent_text,
            previous_situation=prev_text,
            current_time=current_time,
        )

        try:
            text = await self.client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=1024,
            )
            text = text.strip()
            # 空响应重试一次
            if not text:
                logger.warning("Situation returned empty, retrying...")
                text = await self.client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.5,
                    max_tokens=1024,
                )
                text = text.strip()
        except Exception as e:
            logger.error(f"Situation generation failed: {e}")
            if prev_situation:
                return prev_situation
            text = "态势生成失败，请稍后刷新。"

        # 如果 LLM 仍返回空，用事件标题兜底拼接
        if not text:
            active_list = sorted(
                active_events.values(), key=lambda e: e.significance, reverse=True
            )
            parts = [f"当前共有 {len(active_list)} 个活跃事件"]
            top_events = active_list[:5]
            if top_events:
                parts.append("要点包括: " + "; ".join(
                    ev.title for ev in top_events
                ))
            text = "。".join(parts) + "。"
            logger.warning("Using fallback situation text (LLM returned empty)")

        # 提取活跃事件的关键主线
        key_themes: list[str] = []
        theme_count: dict[str, int] = {}
        for ev in active_events.values():
            for th in ev.themes or []:
                theme_count[th] = theme_count.get(th, 0) + 1
        key_themes = sorted(theme_count, key=theme_count.get, reverse=True)[:5]

        sit = Situation(
            generated_at=utcnow_iso(),
            text=text,
            since=utcnow_iso(),
            active_event_count=len(active_events),
            key_themes=key_themes,
        )

        if prev_situation:
            sit.last_telegram_digest_at = prev_situation.last_telegram_digest_at
            sit.morning_brief_date = prev_situation.morning_brief_date
            sit.cross_analysis = prev_situation.cross_analysis
            sit.trend_spotting = prev_situation.trend_spotting

        logger.info(
            f"Situation generated: {len(active_events)} active events, "
            f"top themes: {key_themes[:3]}"
        )
        return sit
