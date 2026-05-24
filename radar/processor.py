"""MiniMax 两段式处理：投资相关性筛选 + 深度信号提取"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from radar.models import Item, Event, utcnow_iso, compute_effective_score
from radar.minimax_client import MinimaxClient
from radar.config import format_coverage_for_prompt, format_themes_for_prompt

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 每批最多处理条目数
_TRIAGE_BATCH_SIZE = 40


def _load_prompt(name: str) -> str:
    """加载 prompt 模板文件"""
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


class Processor:
    """两段式 LLM 处理引擎"""

    def __init__(self, client: MinimaxClient, cfg: dict):
        self.client = client
        self.cfg = cfg
        self.min_score = cfg["scoring"]["min_score_to_keep"]
        self.max_items = cfg["scoring"].get("max_items_in_brief", 25)
        # 预计算合法值集合，用于校验 LLM 输出
        self._valid_tickers = {c["name"] for c in cfg["coverage"]}
        # 别名 → 标准名 映射（LLM 可能用中文别名）
        self._alias_to_name: dict[str, str] = {}
        for c in cfg["coverage"]:
            for alias in c.get("aliases", []) or []:
                self._alias_to_name[alias] = c["name"]
        self._valid_themes = {t["key"] for t in cfg["themes"]}

    # ================================================================
    # 后处理校验：过滤 LLM 幻觉的 ticker/theme
    # ================================================================

    def _resolve_ticker(self, name: str) -> str | None:
        """将 LLM 返回的名称（可能是别名）解析为标准名，无法匹配则返回 None"""
        if name in self._valid_tickers:
            return name
        return self._alias_to_name.get(name)  # None 表示真正的幻觉

    def _validate_item(self, item: Item, stage: str = "triage") -> Item:
        """校验并清洗 LLM 输出的 tickers 和 themes，别名自动映射为标准名"""
        if item.tickers and isinstance(item.tickers, list):
            clean: list[str] = []
            for t in item.tickers:
                resolved = self._resolve_ticker(t)
                if resolved:
                    clean.append(resolved)
                else:
                    logger.warning(
                        f"[{stage}] Unknown ticker stripped for {item.id}: {t}"
                    )
            item.tickers = clean
        elif item.tickers and not isinstance(item.tickers, list):
            logger.warning(
                f"[{stage}] Non-list tickers stripped for {item.id}: {type(item.tickers)}"
            )
            item.tickers = []

        if item.themes:
            valid_themes = [t for t in item.themes if t in self._valid_themes]
            hallucinated = set(item.themes) - set(valid_themes)
            if hallucinated:
                logger.warning(
                    f"[{stage}] Hallucinated themes stripped for {item.id}: {hallucinated}"
                )
            item.themes = valid_themes

        # 同样校验 direction 的 key（确保 direction 是 dict 类型）
        if item.direction and isinstance(item.direction, dict):
            clean_direction = {}
            for tk, d in item.direction.items():
                resolved = self._resolve_ticker(tk)
                if resolved:
                    # 别名可能和原 key 不同，合并同标的 direction
                    if resolved not in clean_direction:
                        clean_direction[resolved] = d
                else:
                    logger.warning(
                        f"[{stage}] Unknown direction ticker stripped for {item.id}: {tk}"
                    )
            item.direction = clean_direction
        elif not isinstance(item.direction, dict):
            logger.warning(
                f"[{stage}] Non-dict direction value stripped for {item.id}: {type(item.direction)}"
            )
            item.direction = {}

        return item

    # ================================================================
    # Stage 1: 投资相关性筛选（批量）
    # ================================================================

    async def triage(self, items: list[Item]) -> list[Item]:
        """
        批量评估投资相关性，返回 score >= min_score 的条目（已填充 score/tickers/themes/relevance_reason）。
        按 score 降序排列，最多取 max_items_in_brief 条。
        """
        if not items:
            return []

        coverage_text = format_coverage_for_prompt(self.cfg)
        themes_text = format_themes_for_prompt(self.cfg)
        template = _load_prompt("triage")

        all_scored: list[dict] = []

        # 分批处理
        for i in range(0, len(items), _TRIAGE_BATCH_SIZE):
            batch = items[i : i + _TRIAGE_BATCH_SIZE]
            batch_json = json.dumps(
                [{"id": it.id, "title": it.title, "summary": it.raw_summary} for it in batch],
                ensure_ascii=False,
            )

            prompt = template.format(
                coverage_list=coverage_text,
                themes_list=themes_text,
                items_json=batch_json,
            )

            try:
                result = await self.client.chat_json(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=8192,
                )
                if isinstance(result, list):
                    all_scored.extend(result)
                elif isinstance(result, dict):
                    # 单个结果也兼容
                    all_scored.append(result)
            except Exception as e:
                logger.error(f"Triage batch {i // _TRIAGE_BATCH_SIZE + 1} failed: {e}")
                continue

        logger.info(f"Triage: {len(items)} items → {len(all_scored)} scored")

        # 将评分结果映射回 Item
        scored_map: dict[str, dict] = {}
        for s in all_scored:
            if isinstance(s, dict) and "id" in s:
                scored_map[s["id"]] = s

        scored_items: list[Item] = []
        for item in items:
            s = scored_map.get(item.id)
            if s is None:
                logger.warning(f"Triage: item {item.id} not in LLM response, silently dropped")
                continue
            try:
                score = int(s.get("score", 0))
            except (ValueError, TypeError):
                logger.warning(f"Triage: non-numeric score for {item.id}: {s.get('score')}")
                continue
            if score < self.min_score:
                continue
            item.relevance_score = score
            item.relevance_reason = s.get("one_line", "")
            item.tickers = s.get("tickers", []) or []
            item.themes = s.get("themes", []) or []
            self._validate_item(item, stage="triage")
            scored_items.append(item)

        # 按时间衰减后的有效分数排序 + 截断
        half_life = self.cfg["scoring"].get("time_decay", {}).get("half_life_hours", 4)
        scored_items.sort(key=lambda x: compute_effective_score(x, half_life), reverse=True)
        kept = scored_items[: self.max_items]

        logger.info(
            f"Triage result: {len(scored_items)} pass threshold → keeping top {len(kept)}"
        )
        return kept

    # ================================================================
    # Stage 2: 深度信号提取（逐条或小批量）
    # ================================================================

    async def extract(self, items: list[Item]) -> list[Item]:
        """对幸存条目做深度信号提取，补全 cn_summary / direction / so_what（并行化）"""
        if not items:
            return []

        coverage_text = format_coverage_for_prompt(self.cfg)
        themes_text = format_themes_for_prompt(self.cfg)
        template = _load_prompt("extract")
        processed_at = utcnow_iso()
        sem = asyncio.Semaphore(5)

        async def _extract_one(item: Item):
            item_json = json.dumps(
                {
                    "id": item.id,
                    "title": item.title,
                    "source": item.source,
                    "summary": item.raw_summary,
                },
                ensure_ascii=False,
            )

            prompt = template.format(
                item_json=item_json,
                coverage_list=coverage_text,
                themes_list=themes_text,
            )

            async with sem:
                try:
                    result = await self.client.chat_json(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3,
                        max_tokens=4096,
                    )
                    if isinstance(result, dict):
                        item.cn_summary = result.get("cn_summary", "") or ""
                        # 合并而非覆盖：extract 新增 tickers/themes，不丢弃 triage 已有的
                        ext_tickers = result.get("tickers", []) or []
                        if isinstance(ext_tickers, list):
                            item.tickers = list(set((item.tickers or []) + ext_tickers))
                        ext_themes = result.get("themes", []) or []
                        if isinstance(ext_themes, list):
                            item.themes = list(set((item.themes or []) + ext_themes))
                        item.direction = result.get("direction", {}) or {}
                        item.so_what = result.get("so_what", "") or ""
                        item.is_primary_source = result.get("is_primary_source", True)
                        item.original_source_url = result.get("original_source_url", "") or ""
                        item.processed_at = processed_at
                        self._validate_item(item, stage="extract")
                    else:
                        logger.warning(f"Extract returned non-dict for {item.id}: {type(result)}")
                except Exception as e:
                    logger.error(f"Extract failed for {item.id}: {e}")
                    # 即使 extract 失败也保留条目（已有 triage 评分）
                    item.processed_at = processed_at

        await asyncio.gather(*[_extract_one(it) for it in items])

        logger.info(f"Extract: processed {len(items)} items")
        return items

    # ================================================================
    # Stage 3: 交叉综合分析 —— 上帝视角的元分析
    # ================================================================

    async def cross_analyze(self, items: list[Item]) -> str:
        """
        对所有已提取条目做交叉综合分析：矛盾、趋势、盲点、联动。
        每轮必跑以最大化 MiniMax 用量。
        Returns:
            综合分析文本（≤500字）
        """
        if not items:
            return ""

        template = _load_prompt("cross_analysis")

        items_json = json.dumps(
            [
                {
                    "id": it.id,
                    "title": it.title,
                    "source": it.source,
                    "credibility": it.credibility,
                    "cn_summary": it.cn_summary,
                    "tickers": it.tickers,
                    "themes": it.themes,
                    "direction": it.direction,
                    "so_what": it.so_what,
                    "score": it.relevance_score,
                }
                for it in items
            ],
            ensure_ascii=False,
        )

        prompt = template.format(all_items_json=items_json)

        try:
            text = await self.client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=1024,
            )
            text = text.strip()
            if text:
                logger.info(f"Cross-analysis generated: {len(text)} chars")
            else:
                logger.warning("Cross-analysis returned empty")
            return text
        except Exception as e:
            logger.error(f"Cross-analysis failed: {e}")
            return ""

    # ================================================================
    # Stage 4: 趋势发现 —— 识别新兴趋势与早期信号
    # ================================================================

    async def trend_spotting(self, items: list[Item]) -> str:
        """
        从所有已处理条目中识别新兴趋势、早期信号和潜在拐点。
        视角：72小时内哪些变化最值得关注？什么信号被市场低估？

        Returns:
            趋势分析文本（≤300字）
        """
        if not items:
            return ""

        template = _load_prompt("trend_spotting")

        items_json = json.dumps(
            [
                {
                    "title": it.title,
                    "cn_summary": it.cn_summary,
                    "tickers": it.tickers,
                    "themes": it.themes,
                    "direction": it.direction,
                    "so_what": it.so_what,
                    "score": it.relevance_score,
                }
                for it in items[:30]
            ],
            ensure_ascii=False,
        )

        prompt = template.format(items_json=items_json)

        try:
            text = await self.client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=800,
            )
            text = text.strip()
            if text:
                logger.info(f"Trend spotting generated: {len(text)} chars")
            return text
        except Exception as e:
            logger.error(f"Trend spotting failed: {e}")
            return ""

    # ================================================================
    # Stage 2.5: 视觉富化 —— 对高分条目做图片理解
    # ================================================================

    async def visual_enrich(self, items: list[Item], max_images: int | None = None) -> None:
        """
        对高相关性条目（score >= min_score 且有配图）调用图片理解 API，
        提取图表、产品图、架构图中的关键信息。

        Args:
            items:        已处理的 Item 列表
            max_images:   每轮最多分析的图片数（None = 读配置）
        """
        ve_cfg = self.cfg["scoring"].get("visual_enrich", {})
        if max_images is None:
            max_images = ve_cfg.get("max_images", 5)
        min_score = ve_cfg.get("min_score", 7)

        candidates = [
            it for it in items
            if it.image_url and it.relevance_score >= min_score and not it.visual_analysis
        ]
        if not candidates:
            return

        candidates.sort(key=lambda x: x.relevance_score, reverse=True)
        batch = candidates[:max_images]

        logger.info(
            f"Visual enrich: analyzing {len(batch)} images "
            f"(from {len(candidates)} candidates, quota limit {max_images})"
        )

        prompt = (
            "你是一位专业的科技/投资研究助手。请分析这张图片的内容，重点关注："
            "1. 是否有图表/数据可视化？如有，概述其核心发现"
            "2. 是否有产品图/硬件图？描述关键特征"
            "3. 是否有架构图/流程图？概括其核心思想"
            "4. 图片传达了什么文字之外的信息？"
            "用中文回答，≤100字，只输出分析结果，不要客套话。"
        )

        for item in batch:
            try:
                result = await self.client.understand_image(
                    prompt=prompt,
                    image_url=item.image_url,
                )
                if result:
                    item.visual_analysis = result.strip()
                    logger.info(
                        f"Visual enrich: {item.id[:12]} score={item.relevance_score} "
                        f"→ {len(result)} chars"
                    )
            except Exception as e:
                logger.error(f"Visual enrich failed for {item.id[:12]}: {e}")

    # ================================================================
    # Stage 5: 事件深度分析
    # ================================================================

    async def event_deep_dive(self, event: Event, event_items: list[Item]) -> str:
        """
        对事件进行深度分析：看多逻辑、看空风险、关键驱动因素、市场影响、时间线推断。

        Args:
            event:       事件对象
            event_items: 该事件下的条目列表

        Returns:
            深度分析文本（≤600字中文），失败返回空字符串
        """
        if not event_items:
            return ""

        template = _load_prompt("event_deep_dive")

        event_json = json.dumps(
            {
                "title": event.title,
                "summary": event.summary,
                "tickers": event.tickers,
                "themes": event.themes,
                "significance": event.significance,
                "status": event.status,
            },
            ensure_ascii=False,
        )

        # 取 top 10 高分条目
        top_items = sorted(event_items, key=lambda x: x.relevance_score, reverse=True)[:10]
        items_json = json.dumps(
            [
                {
                    "title": it.title,
                    "cn_summary": it.cn_summary,
                    "so_what": it.so_what,
                    "tickers": it.tickers,
                    "themes": it.themes,
                    "direction": it.direction,
                    "credibility": it.credibility,
                    "score": it.relevance_score,
                }
                for it in top_items
            ],
            ensure_ascii=False,
        )

        prompt = template.format(event_json=event_json, items_json=items_json)

        try:
            text = await self.client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=1024,
            )
            text = text.strip()
            if text:
                logger.info(f"Event deep dive for {event.event_id}: {len(text)} chars")
            return text
        except Exception as e:
            logger.error(f"Event deep dive failed for {event.event_id}: {e}")
            return ""

    # ================================================================
    # Stage 6: 反向观点分析
    # ================================================================

    async def second_opinion(self, items: list[Item]) -> None:
        """
        对高分条目（score >= 7 且已有 cn_summary）生成反向观点，
        提供替代解读或指出遗漏点。

        Args:
            items: 已处理的 Item 列表（原地修改 second_opinion 字段）
        """
        candidates = [
            it for it in items
            if it.relevance_score >= 7 and it.cn_summary and not it.second_opinion
        ]
        if not candidates:
            return

        template = _load_prompt("second_opinion")
        sem = asyncio.Semaphore(5)

        async def _analyze_one(item: Item):
            items_json = json.dumps(
                {
                    "title": item.title,
                    "cn_summary": item.cn_summary,
                    "so_what": item.so_what,
                    "tickers": item.tickers,
                    "themes": item.themes,
                },
                ensure_ascii=False,
            )

            prompt = template.format(items_json=items_json)

            async with sem:
                try:
                    text = await self.client.chat(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.4,
                        max_tokens=512,
                    )
                    if text:
                        item.second_opinion = text.strip()
                except Exception as e:
                    logger.error(f"Second opinion failed for {item.id[:12]}: {e}")

        await asyncio.gather(*[_analyze_one(it) for it in candidates])
        count = sum(1 for it in candidates if it.second_opinion)
        logger.info(f"Second opinion: {count}/{len(candidates)} items analyzed")

    # ================================================================
    # 组合: triage + extract
    # ================================================================

    async def process(self, items: list[Item]) -> list[Item]:
        """完整两段式处理: triage → extract，返回成品 Item 列表"""
        triaged = await self.triage(items)
        if not triaged:
            return []
        return await self.extract(triaged)
