"""入选文章的正文抓取

RSS 的 raw_summary 常常只是导语(上限 1500 字符, 很多源给的是一两句话),
拿它写"作者没说的/薄弱处"只能写出套话。入选条目每天不过 daily_limit 篇,
值得为它们多发几个请求换一份读得出深浅的笔记。

抓不到就退回 raw_summary —— 少一份全文不该让整条管道停下。
"""

import asyncio
import logging

import httpx

# 正文抽取本身与"阅读流"无关, 已提到共享模块 —— SEC 采集器也要用同一套
from radar.htmltext import extract_text  # noqa: F401  (对外仍从本模块导出)

logger = logging.getLogger(__name__)

_TIMEOUT = 20
_CONCURRENCY = 3

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ai-research-radar/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}


async def fetch_one(url: str) -> str:
    """抓取单篇正文, 失败返回空串(调用方退回 raw_summary)"""
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype.lower():
                logger.info(f"Fulltext skipped (not html: {ctype}): {url}")
                return ""
            return extract_text(resp.text)
    except Exception as e:
        logger.info(f"Fulltext fetch failed ({type(e).__name__}): {url}")
        return ""


async def enrich(candidates: list[dict]) -> int:
    """就地给候选补 fulltext 字段, 返回成功抓到的篇数"""
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(cand: dict):
        async with sem:
            cand["fulltext"] = await fetch_one(cand.get("url", ""))

    await asyncio.gather(*[_one(c) for c in candidates], return_exceptions=True)
    ok = sum(1 for c in candidates if c.get("fulltext"))
    logger.info(f"Fulltext: {ok}/{len(candidates)} articles fetched")
    return ok
