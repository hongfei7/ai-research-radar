"""发送传输层 —— WeCom webhook / Telegram / GitHub Issue

- WeCom: 每条消息独立 POST, 顺序发送保证阅读顺序, 重试 3 次
- 消息预算由调用方(scheduler/run)控制, 本层只负责可靠投递
"""

import asyncio
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_WECOM_MAX_BYTES = 4000  # WeCom 硬上限 4096, 渲染层已留 margin, 这里兜底校验


async def _post_with_retry(url: str, body: bytes, attempts: int = 3) -> Optional[dict]:
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    url, content=body,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"POST failed (attempt {attempt + 1}): {e}")
            if attempt < attempts - 1:
                await asyncio.sleep(2)
    return None


async def send_wecom_messages(messages: list[str]) -> int:
    """顺序发送多条 WeCom markdown 消息, 返回成功条数"""
    webhook_url = os.environ.get("WECOM_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("WECOM_WEBHOOK_URL not set, skipping WeCom push")
        return 0

    sent = 0
    for msg in messages:
        encoded = msg.encode("utf-8")
        if len(encoded) > _WECOM_MAX_BYTES:
            # 渲染层失守的兜底: 字节截断
            msg = encoded[:_WECOM_MAX_BYTES].decode("utf-8", errors="ignore")
            logger.warning("WeCom message exceeded budget at transport layer, truncated")
        payload = {"msgtype": "markdown", "markdown": {"content": msg}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result = await _post_with_retry(webhook_url, body)
        if result and result.get("errcode") == 0:
            sent += 1
        else:
            logger.error(f"WeCom API error: {result}")
        if len(messages) > 1:
            await asyncio.sleep(0.5)  # 保证顺序, 远离频率限制
    logger.info(f"WeCom: {sent}/{len(messages)} messages sent")
    return sent


async def send_telegram_html(text: str) -> bool:
    """发送 Telegram HTML 消息"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = await _post_with_retry(url, body, attempts=2)
    ok = bool(result and result.get("ok"))
    if not ok:
        logger.error(f"Telegram API error: {result}")
    return ok


async def find_today_issue(title_prefix: str, label: str = "晨报") -> Optional[str]:
    """幂等: 查找已存在的同题 Issue, 返回 URL(审计 H2)"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not repo or not token:
        return None
    url = f"{api_url}/repos/{repo}/issues"
    params = {"state": "all", "labels": label, "per_page": 5}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url, params=params,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            for issue in resp.json():
                if title_prefix in (issue.get("title") or ""):
                    return issue.get("html_url")
    except Exception as e:
        logger.warning(f"find_today_issue failed: {e}")
    return None


async def create_issue(title: str, body: str, labels: list[str]) -> Optional[str]:
    """创建 GitHub Issue, 返回 URL"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not repo or not token:
        logger.warning("GITHUB_REPOSITORY or GITHUB_TOKEN not set, skipping issue")
        return None
    url = f"{api_url}/repos/{repo}/issues"
    payload = {"title": title, "body": body, "labels": labels}
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url, content=body_bytes,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json; charset=utf-8"},
            )
            resp.raise_for_status()
            return resp.json().get("html_url")
    except Exception as e:
        logger.error(f"Failed to create issue: {e}")
        return None
