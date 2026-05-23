"""GitHub Trending 采集器 —— HTML 抓取 + selectolax 解析"""

import logging

import httpx
from selectolax.parser import HTMLParser

from radar.collectors.base import Collector
from radar.collectors.rss import make_id, normalize_url
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred

logger = logging.getLogger(__name__)

_GITHUB_TRENDING = "https://github.com/trending"
_TIMEOUT = 30
_MAX_RAW_SUMMARY = 1500


class GithubTrendingCollector(Collector):
    """GitHub Trending —— 抓取 trending 页面，selectolax 解析"""

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        language = params.get("language", "")
        since = params.get("since", "daily")
        max_items = int(params.get("max", 25))
        fetched_at = utcnow_iso()

        # 构造 URL
        url = _GITHUB_TRENDING
        if language:
            url += f"/{language}"
        url += f"?since={since}"

        logger.debug(f"[{source_id}] Fetching {url}")

        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            try:
                resp = await client.get(
                    url,
                    headers={
                        "Accept": "text/html",
                        "User-Agent": "ai-research-radar/1.0",
                    },
                )
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                logger.error(f"[{source_id}] HTTP fetch failed: {e}")
                return []

        try:
            items = self._parse(html, source_id, fetched_at, max_items)
        except Exception as e:
            logger.error(f"[{source_id}] Parse failed: {e}", exc_info=True)
            return []

        logger.info(f"[{source_id}] Fetched {len(items)} trending repos")
        return items

    def _parse(self, html: str, source_id: str, fetched_at: str, max_items: int) -> list[Item]:
        tree = HTMLParser(html)
        items: list[Item] = []

        # GitHub Trending 的 repo 卡片在 article.Box-row 中
        repo_cards = tree.css("article.Box-row")
        if not repo_cards:
            # 降级：尝试其他选择器
            repo_cards = tree.css("div.Box article.Box-row")
        if not repo_cards:
            repo_cards = tree.css(".Box-row")

        for card in repo_cards[:max_items]:
            try:
                # 仓库名: h2 a 或 h1 a
                title_el = (
                    card.css_first("h2 a")
                    or card.css_first("h1 a")
                    or card.css_first("a[href*='github.com']")
                )
                if not title_el:
                    continue

                href = title_el.attributes.get("href", "")
                # 清理: "/owner/repo" → full URL
                repo_path = href.strip().lstrip("/")
                if not repo_path:
                    continue
                repo_url = f"https://github.com/{repo_path}"

                # 标题: owner / repo 格式
                title_text = title_el.text(strip=True) or repo_path
                # 去除多余空白和换行
                title_text = " ".join(title_text.split())

                # 描述: p 标签
                desc_el = card.css_first("p")
                description = desc_el.text(strip=True) if desc_el else ""

                # 编程语言
                lang_el = card.css_first("[itemprop='programmingLanguage']")
                lang = lang_el.text(strip=True) if lang_el else ""

                # 今日 star 数
                stars_today = ""
                for el in card.css("span"):
                    text = el.text(strip=True)
                    if "star" in text.lower():
                        stars_today = text
                        break

                # 构造摘要
                parts = [f"GitHub Trending: {title_text}"]
                if description:
                    parts.append(description)
                if lang:
                    parts.append(f"Language: {lang}")
                if stars_today:
                    parts.append(stars_today)
                raw_summary = ". ".join(parts)[:_MAX_RAW_SUMMARY]

                item = Item(
                    id=make_id(repo_url),
                    title=title_text,
                    url=normalize_url(repo_url),
                    source=source_id,
                    source_type="tech",
                    published_at=fetched_at,
                    fetched_at=fetched_at,
                    raw_summary=raw_summary,
                    credibility=_source_cred(source_id),
                )
                items.append(item)
            except Exception as e:
                logger.warning(f"[{source_id}] Failed to parse repo card: {e}")
                continue

        return items
