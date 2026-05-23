"""HuggingFace Daily Papers 采集器 —— 通过公开 JSON API 获取每日推荐论文"""

import hashlib
import logging
from datetime import datetime, timezone

import httpx

from radar.collectors.base import Collector
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred

logger = logging.getLogger(__name__)

_API_URL = "https://huggingface.co/api/daily_papers"
_TIMEOUT = 30
_MAX_RAW_SUMMARY = 1500


def _make_id(paper_id: str) -> str:
    return hashlib.sha1(f"hf:{paper_id}".encode("utf-8")).hexdigest()


def _arxiv_url(paper_id: str) -> str:
    return f"https://arxiv.org/abs/{paper_id}"


class HuggingFacePapersCollector(Collector):
    """从 HuggingFace Daily Papers JSON API 采集当日推荐论文"""

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        max_papers = params.get("max", 20)
        fetched_at = utcnow_iso()
        items: list[Item] = []

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; ai-research-radar/1.0; +https://github.com/USER/ai-research-radar)",
            }
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
                resp = await client.get(_API_URL, params={"limit": max_papers})
                resp.raise_for_status()
                papers = resp.json()
        except Exception as e:
            logger.error(f"[{source_id}] Failed to fetch daily papers: {e}")
            return []

        if not isinstance(papers, list):
            logger.warning(f"[{source_id}] Unexpected response format: {type(papers)}")
            return []

        for p in papers:
            try:
                if not isinstance(p, dict):
                    continue
                paper = p.get("paper", p)
                paper_id = paper.get("id", "")
                if not paper_id:
                    continue

                title = (p.get("title") or paper.get("title") or "").strip()
                summary = (p.get("summary") or paper.get("summary") or "").strip()

                # 使用 ai_summary 作为 raw_summary（更精炼）
                ai_summary = paper.get("ai_summary", "")
                raw = ai_summary if ai_summary else summary
                raw = raw[:_MAX_RAW_SUMMARY]

                # 解析 publishedAt
                pub_date = p.get("publishedAt") or paper.get("publishedAt") or fetched_at
                try:
                    dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                    pub_date = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    pub_date = fetched_at

                # 提取缩略图 URL（论文关键图）
                thumbnail = p.get("thumbnail", "") or paper.get("thumbnail", "")
                # 提取 GitHub repo 链接
                gh_repo = paper.get("githubRepo", "")

                item = Item(
                    id=_make_id(paper_id),
                    title=title,
                    url=_arxiv_url(paper_id),
                    source=source_id,
                    source_type="tech",
                    published_at=pub_date,
                    fetched_at=fetched_at,
                    raw_summary=raw,
                    credibility=_source_cred(source_id),
                    image_url=thumbnail,
                )
                items.append(item)
            except Exception as e:
                logger.warning(f"[{source_id}] Failed to process paper: {e}")
                continue

        logger.info(f"[{source_id}] Fetched {len(items)} daily papers")
        return items
