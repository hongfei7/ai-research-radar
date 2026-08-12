"""事件聚类引擎 —— 将同事件多来源报道合并为事件线"""

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Optional

from radar.models import Item, Event, utcnow_iso
from radar.minimax_client import MinimaxClient, cosine_similarity
from radar.prompts import load_prompt
from radar.textnorm import clean_title

logger = logging.getLogger(__name__)

# 合并闸门：中文 bigram 的 Jaccard 天然偏低，取值不能照搬英文分词的经验值
_MIN_WORD_OVERLAP = 0.12
_BIG_EVENT_SOURCES = 8              # 超过此来源数视为"大事件"，收紧合并
_BIG_EVENT_MIN_WORD_OVERLAP = 0.20


def _generate_event_id(title: str) -> str:
    """为事件生成简短 ID"""
    h = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    return f"evt_{h}"


def _safe_int(value, default: int = 0) -> int:
    """安全转换为 int，失败返回 default"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _recompute_tickers(counts: dict, source_count: int, max_tickers: int) -> list[str]:
    """从标的命中频次收敛出事件的代表标的

    直接 union 会让长命事件吸附整个覆盖池（实测单事件挂过 33 个标的，
    附录里的"标的"列因此变成噪音，且 _find_match_keyword 的 ticker 交集
    判断会退化——标的越多越容易匹配，形成越滚越大的垃圾桶事件）。
    这里要求标的至少在 1/3 的来源中出现，再按频次取前 N。
    """
    if not counts:
        return []
    support = max(1, math.ceil(max(source_count, 1) / 3))
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    kept = [t for t, c in ranked if c >= support][:max_tickers]
    if not kept:
        kept = [ranked[0][0]]  # 支持度门槛过高时至少保留最高频的一个
    return kept


def _recompute_significance(max_item_score: int, source_count: int) -> int:
    """重要性 = 最高条目分 + 多源加成，上限 10

    原实现是 max(历史值, LLM 本轮给的值)，只增不减，配合每轮重写迅速通胀到
    满分（实测活跃事件里 8 分以上占一半，"小米手机搭载自研芯片"拿到 10/10）。
    """
    base = max(0, min(10, _safe_int(max_item_score, 0)))
    bonus = min(2, max(0, source_count - 1) // 2)
    return min(10, base + bonus)


def _tokenize_for_matching(text: str) -> set[str]:
    """将文本拆分为可用于 Jaccard 比较的特征集

    中英混合文本：CJK 字符 bigram + 英文/数字完整单词。
    """
    if not text:
        return set()
    text = text.lower().strip()
    features: set[str] = set()

    # CJK 字符 bigram
    cjk = re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]', text)
    for i in range(len(cjk) - 1):
        features.add(cjk[i] + cjk[i + 1])

    # 英文/数字单词（≥2 字符，跳过纯数字）
    for w in re.findall(r'[a-z0-9][a-z0-9.\-]*[a-z0-9]|[a-z0-9]{2,}', text):
        if not re.fullmatch(r'[\d.\-]+', w):
            features.add(w)

    return features


class ClusterEngine:
    """事件聚类引擎：

    1. 对新条目获取 embedding
    2. 与现有活跃事件比较余弦相似度
    3. 相似 → 合并；否则 → 创建新事件
    4. 合并后可选 LLM 重写事件摘要
    """

    def __init__(self, client: MinimaxClient, cfg: dict):
        self.client = client
        self.cfg = cfg
        self.similarity_threshold = cfg["clustering"].get("similarity_threshold", 0.85)
        self.max_active_events = cfg["clustering"].get("max_active_events", 30)
        self.event_ttl_hours = cfg["clustering"].get("event_ttl_hours", 24)
        self.max_tickers_per_event = cfg["clustering"].get("max_tickers_per_event", 4)
        self._valid_themes = {t["key"] for t in cfg.get("themes", [])}

    async def cluster(
        self,
        items: list[Item],
        existing_events: dict[str, Event],
    ) -> tuple[list[Item], dict[str, Event]]:
        """
        对处理后的条目进行事件聚类。

        Args:
            items:            已处理的 Item 列表（含 cn_summary）
            existing_events:  当前事件状态

        Returns:
            (更新后的 items, 更新后的 events)
        """
        if not items:
            return items, existing_events

        # Step 1: 关键词匹配聚类（MiniMax M2.7 不含 Embeddings API）
        # embedding 方案留待后续套餐升级后启用

        # Step 2: 对每条新条目做匹配
        events = dict(existing_events)  # 复制，避免修改原始
        now = utcnow_iso()
        updated_event_ids: set[str] = set()  # 收集被更新的事件

        for item in items:
            matched = self._find_match_keyword(item, events)

            if matched:
                # 合并到已有事件
                event_id = matched
                event = events[event_id]
                event.item_ids.append(item.id)
                event.source_count = len(set(event.item_ids))
                event.last_updated_at = now
                event.is_active = True
                # tickers 走频次收敛而非 union; themes 去重排序
                for tk in (item.tickers or []):
                    event.ticker_counts[tk] = event.ticker_counts.get(tk, 0) + 1
                event.tickers = _recompute_tickers(
                    event.ticker_counts, event.source_count, self.max_tickers_per_event
                )
                event.themes = sorted(set(event.themes) | set(item.themes or []))
                event.max_item_score = max(
                    _safe_int(event.max_item_score, 0), _safe_int(item.relevance_score, 0)
                )
                event.significance = _recompute_significance(
                    event.max_item_score, event.source_count
                )
                item_dir = item.direction if isinstance(item.direction, dict) else {}
                for tk, d in item_dir.items():
                    if tk not in event.direction:
                        event.direction[tk] = d

                item.event_id = event_id
                item.is_new_event = False
                item.is_event_update = True

                # （embedding 更新留待后续套餐升级后启用）
                # if use_embeddings and event.embedding and emb:
                #     event.embedding = [
                #         (a + b) / 2 for a, b in zip(event.embedding, emb)
                #     ]

                updated_event_ids.add(event_id)

            else:
                # 创建新事件
                event_id = _generate_event_id(item.cn_summary or item.title)
                ticker_counts = {tk: 1 for tk in (item.tickers or [])}
                event = Event(
                    event_id=event_id,
                    title=item.title,
                    summary=item.cn_summary,
                    tickers=_recompute_tickers(ticker_counts, 1, self.max_tickers_per_event),
                    themes=sorted(set(item.themes or [])),
                    direction=dict(item.direction or {}),
                    item_ids=[item.id],
                    source_count=1,
                    first_seen_at=now,
                    last_updated_at=now,
                    is_active=True,
                    significance=_recompute_significance(item.relevance_score, 1),
                    status="developing",
                    ticker_counts=ticker_counts,
                    max_item_score=_safe_int(item.relevance_score, 0),
                    embedding=None,  # 留待后续套餐升级后启用
                )
                events[event_id] = event
                item.event_id = event_id
                item.is_new_event = True
                item.is_event_update = False

        # Step 2.5: 对每个被更新的事件，合并所有新条目后重写一次
        new_items_by_event: dict[str, list[Item]] = {}
        for it in items:
            if it.is_event_update and it.event_id:
                new_items_by_event.setdefault(it.event_id, []).append(it)
        for event_id, new_items in new_items_by_event.items():
            event = events.get(event_id)
            if event:
                await self._rewrite_event(event, new_items, events)

        # Step 3: 清理过期事件
        events = self._cleanup_stale(events, now)

        # Step 4: 限制活跃事件数量
        events = self._trim_events(events)

        logger.info(
            f"Clustering: {len(items)} items → "
            f"{len(events)} active events "
            f"(new: {sum(1 for it in items if it.is_new_event)}, "
            f"updated: {sum(1 for it in items if it.is_event_update)})"
        )

        return items, events

    def _find_match_embedding(self, emb: list[float], events: dict[str, Event]) -> Optional[str]:
        """基于 embedding 余弦相似度寻找匹配事件（预留，当前套餐不含 Embeddings API）"""
        best_id = None
        best_score = 0.0

        for eid, event in events.items():
            if not event.is_active:
                continue
            if not event.embedding:  # None 或空列表均跳过
                continue
            sim = cosine_similarity(emb, event.embedding)
            if sim > best_score:
                best_score = sim
                best_id = eid

        if best_score >= self.similarity_threshold:
            return best_id
        return None

    def _find_match_keyword(self, item: Item, events: dict[str, Event]) -> Optional[str]:
        """基于 ticker/theme/标题关键词重叠匹配事件。

        设计要点:
        - ticker 权重最高（0.5），同一标的的新闻才可能同事件
        - theme + 关键词权重各 0.25，辅助区分同标的不同事件
        - 阈值 0.50：防止宽泛主题（如 compute_demand）导致误合并
        - 文本必须有最低重叠：只靠 ticker+theme 重叠就能达到 0.5，会把
          "台积电业绩超预期"和"台积电封装扩产"并成一条（实测出现过一个
          事件吞下 7 个来源、33 个标的），所以额外要求 word_overlap 下限
        """
        item_tickers = set(item.tickers or [])
        item_themes = set(item.themes or [])
        item_words = _tokenize_for_matching(item.cn_summary or item.title)

        best_id = None
        best_score = 0.0

        for eid, event in events.items():
            if not event.is_active:
                continue

            ev_tickers = set(event.tickers or [])
            ev_themes = set(event.themes or [])
            ev_words = _tokenize_for_matching(event.summary or event.title)

            # Ticker 重叠检查：双方都有 ticker 时强制要求交集
            if item_tickers and ev_tickers:
                if not (item_tickers & ev_tickers):
                    continue
                ticker_overlap = len(item_tickers & ev_tickers) / max(len(item_tickers | ev_tickers), 1)
            else:
                # 至少一方没有 ticker（如 OpenAI/DeepSeek），不强制但无加分
                ticker_overlap = 0.0

            theme_overlap = len(item_themes & ev_themes) / max(len(item_themes | ev_themes), 1)
            word_overlap = len(item_words & ev_words) / max(len(item_words | ev_words), 1)

            # 文本重叠下限：讲同一件事必然共用一批词
            if word_overlap < _MIN_WORD_OVERLAP:
                continue
            # 已经很大的事件要求更强的文本证据，否则它会持续吸附无关条目
            if event.source_count >= _BIG_EVENT_SOURCES and word_overlap < _BIG_EVENT_MIN_WORD_OVERLAP:
                continue

            # ticker 权重最高 → 同标的才可能同事件
            score = ticker_overlap * 0.5 + theme_overlap * 0.25 + word_overlap * 0.25

            if score > best_score:
                best_score = score
                best_id = eid

        keyword_threshold = 0.50  # n-gram 对中文 Jaccard 偏低，适度放宽
        if best_score >= keyword_threshold:
            return best_id
        return None

    async def _rewrite_event(
        self, event: Event, new_items: list[Item], all_events: dict[str, Event]
    ) -> None:
        """当事件有新条目加入时，用 LLM 重写事件标题和摘要（批量合并所有新条目）"""
        template = load_prompt("cluster")

        existing_info = json.dumps(
            {"title": event.title, "summary": event.summary},
            ensure_ascii=False,
        )
        new_items_info = json.dumps(
            [
                {
                    "title": it.title,
                    "cn_summary": it.cn_summary,
                    "tickers": it.tickers,
                    "themes": it.themes,
                    "direction": it.direction,
                    "so_what": it.so_what,
                }
                for it in new_items
            ],
            ensure_ascii=False,
        )

        prompt = template.format(
            existing_event=existing_info,
            new_item_json=new_items_info,
        )

        try:
            result = await self.client.chat_json(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                thinking=False,  # 事件标题/摘要改写, 机械任务
            )
            if isinstance(result, dict):
                event.title = clean_title(result.get("event_title", "") or event.title)
                event.summary = result.get("event_summary", event.summary)
                # tickers / significance 不采纳 LLM 的输出：
                # 前者会重新引入 union 膨胀，后者是重要性通胀的直接来源。
                # 两者都已由成员条目在合并时算好，这里只吸收标题/摘要/状态。
                new_themes = {
                    t for t in (result.get("themes") or []) if t in self._valid_themes
                }
                event.themes = sorted(set(event.themes) | new_themes)
                event.status = result.get("status", event.status)
        except Exception as e:
            logger.error(f"Failed to rewrite event {event.event_id}: {e}")

    def _cleanup_stale(self, events: dict[str, Event], now: str) -> dict[str, Event]:
        """标记超过 TTL 且无更新的活跃事件为 resolved"""
        try:
            now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except Exception:
            return events

        for event in events.values():
            if not event.is_active:
                continue
            try:
                # 如果没有 last_updated_at，回退到 first_seen_at
                ts = event.last_updated_at or event.first_seen_at
                if not ts:
                    continue
                updated = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hours_since = (now_dt - updated).total_seconds() / 3600
                if hours_since >= self.event_ttl_hours:
                    event.is_active = False
                    event.status = "resolved"
                    logger.info(f"Event {event.event_id} resolved (stale {hours_since:.1f}h)")
            except Exception:
                continue

        return events

    def _trim_events(self, events: dict[str, Event]) -> dict[str, Event]:
        """保留最多 max_active_events 个活跃事件（按 significance 排序）"""
        active = {eid: ev for eid, ev in events.items() if ev.is_active}
        if len(active) <= self.max_active_events:
            return events

        # 按 significance 降序保留
        sorted_active = sorted(
            active.items(), key=lambda x: x[1].significance, reverse=True
        )
        keep_ids = {eid for eid, _ in sorted_active[: self.max_active_events]}

        for eid in active:
            if eid not in keep_ids:
                events[eid].is_active = False
                events[eid].status = "resolved"
                logger.info(f"Event {eid} trimmed (exceeded max active)")

        return events
