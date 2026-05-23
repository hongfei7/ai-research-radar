"""MiniMax API 轻量客户端"""

import os
import json
import logging
import asyncio
from typing import Optional, Any

import httpx

logger = logging.getLogger(__name__)

# 环境变量
ENV_API_KEY = "MINIMAX_API_KEY"
ENV_BASE_URL = "MINIMAX_BASE_URL"

# 默认值
DEFAULT_BASE_URL = "https://api.minimax.chat/v1"
DEFAULT_PLAN_BASE_URL = "https://api.minimaxi.com"
DEFAULT_MODEL = "MiniMax-Text-01"
DEFAULT_EMBEDDING_MODEL = "embo-01"

# 重试与超时
MAX_RETRIES = 2           # 解析失败重试次数
REQUEST_TIMEOUT = 120      # 单次请求超时(秒)
TOTAL_TIMEOUT = 300        # 含重试的总超时(秒)


class MinimaxClient:
    """MiniMax API 封装 —— chat completion + embeddings"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or os.environ.get(ENV_API_KEY, "")
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self._client: Optional[httpx.AsyncClient] = None

        if not self.api_key:
            logger.warning(
                f"MINIMAX_API_KEY not set. Set env var {ENV_API_KEY} or pass api_key parameter."
            )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ================================================================
    # Chat Completion
    # ================================================================

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        retries: int = MAX_RETRIES,
    ) -> str:
        """
        调用 chat completion，返回模型生成的文本。

        Args:
            messages:    标准 messages 列表 [{"role":"system/user","content":"..."}]
            model:       模型名，默认 self.model
            temperature: 温度参数
            max_tokens:  最大输出 token
            retries:     JSON 解析失败时的重试次数

        Returns:
            模型生成的原始文本

        Raises:
            RuntimeError: 在重试后仍然失败
        """
        client = await self._get_client()
        url = f"{self.base_url}/text/chatcompletion_v2"

        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_error = None
        for attempt in range(retries + 1):
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

                # MiniMax API 错误: base_resp.status_code != 0
                base_resp = data.get("base_resp", {})
                if base_resp.get("status_code", 0) != 0:
                    err_msg = base_resp.get("status_msg", "unknown error")
                    err_code = base_resp.get("status_code", -1)
                    logger.error(f"MiniMax API error [{err_code}]: {err_msg}")
                    if attempt < retries and err_code in (1001, 1002):  # 可重试的限流错误
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(f"MiniMax API error [{err_code}]: {err_msg}")

                # 标准返回: data.choices[0].message.content
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    return content

                # 兼容其他返回格式
                if "reply" in data:
                    return data["reply"]
                if "data" in data and "reply" in data["data"]:
                    return data["data"]["reply"]

                logger.warning(f"Unexpected chat response format: {json.dumps(data, ensure_ascii=False)[:500]}")
                return ""

            except httpx.HTTPStatusError as e:
                last_error = e
                logger.error(f"Chat API error (attempt {attempt + 1}/{retries + 1}): {e.response.status_code} {e.response.text[:500]}")
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                last_error = e
                logger.error(f"Chat request failed (attempt {attempt + 1}/{retries + 1}): {e}")
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Chat API failed after {retries + 1} attempts: {last_error}")

    async def chat_json(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        retries: int = MAX_RETRIES,
    ) -> dict | list:
        """
        调用 chat completion 并解析为 JSON。

        与 chat() 相同参数，但返回解析后的 JSON 对象。
        如果模型返回非 JSON 文本，尝试从文本中提取 JSON 块。
        """
        text = await self.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            retries=MAX_RETRIES,  # 网络层重试
        )

        for attempt in range(retries + 1):
            try:
                return json.loads(text.strip())
            except json.JSONDecodeError:
                # 尝试提取 ```json ... ``` 代码块
                extracted = _extract_json_block(text)
                if extracted:
                    try:
                        return json.loads(extracted)
                    except json.JSONDecodeError:
                        pass

                if attempt < retries:
                    logger.warning(f"JSON parse failed, retrying... (attempt {attempt + 1}/{retries + 1})")
                    # 追加修正提示重试
                    fix_messages = messages + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": "你的上一次回复不是合法的 JSON。请严格按要求的 JSON 格式重新输出，只输出 JSON，不要加任何其他文字。"},
                    ]
                    text = await self.chat(
                        messages=fix_messages,
                        model=model,
                        temperature=max(0.1, temperature - 0.1),
                        max_tokens=max_tokens,
                        retries=1,  # JSON 修正重试时仍保留网络层重试
                    )
                else:
                    logger.error(f"Failed to parse JSON after {retries + 1} attempts. Raw text: {text[:500]}")
                    raise ValueError(f"Failed to parse JSON response: {text[:300]}")

        return {}

    # ================================================================
    # Embeddings
    # ================================================================

    async def embedding(
        self,
        text: str,
        model: str | None = None,
    ) -> list[float]:
        """
        获取文本的 embedding 向量。

        Args:
            text:  输入文本
            model: embedding 模型名，默认 embo-01

        Returns:
            embedding 向量 (float 列表)，失败返回空列表
        """
        client = await self._get_client()
        url = f"{self.base_url}/embeddings"

        payload = {
            "model": model or DEFAULT_EMBEDDING_MODEL,
            "texts": [text],
            "type": "query",
        }

        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            # 检查 API 业务错误
            base_resp = data.get("base_resp", {})
            if base_resp.get("status_code", 0) != 0:
                err_msg = base_resp.get("status_msg", "unknown error")
                err_code = base_resp.get("status_code", -1)
                logger.error(f"Embedding API error [{err_code}]: {err_msg}")
                return []

            # 标准返回格式: data.vectors[0]
            vectors = data.get("vectors", [])
            if vectors:
                return vectors[0]

            # 兼容格式
            if "data" in data and "vectors" in data["data"]:
                return data["data"]["vectors"][0]

            logger.warning(f"Unexpected embedding response format: {json.dumps(data, ensure_ascii=False)[:500]}")
            return []

        except Exception as e:
            logger.error(f"Embedding request failed: {e}")
            return []

    async def embedding_batch(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """
        批量获取 embedding 向量。

        Args:
            texts: 输入文本列表
            model: embedding 模型名

        Returns:
            embedding 向量列表
        """
        if not texts:
            return []

        client = await self._get_client()
        url = f"{self.base_url}/embeddings"

        payload = {
            "model": model or DEFAULT_EMBEDDING_MODEL,
            "texts": texts,
            "type": "db",
        }

        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            # 检查 API 业务错误
            base_resp = data.get("base_resp", {})
            if base_resp.get("status_code", 0) != 0:
                err_msg = base_resp.get("status_msg", "unknown error")
                err_code = base_resp.get("status_code", -1)
                logger.error(f"Embedding batch API error [{err_code}]: {err_msg}")
                return [[] for _ in texts]

            vectors = data.get("vectors", [])
            if vectors:
                return vectors

            if "data" in data and "vectors" in data["data"]:
                return data["data"]["vectors"]

            return [[] for _ in texts]

        except Exception as e:
            logger.error(f"Embedding batch request failed: {e}")
            return [[] for _ in texts]

    # ================================================================
    # Coding Plan: Web Search (联网搜索)
    # ================================================================

    async def search(
        self,
        query: str,
        plan_base_url: str | None = None,
    ) -> list[dict]:
        """
        调用 MiniMax Coding Plan 联网搜索 API。

        端点: POST /v1/coding_plan/search
        对应 MCP 工具 web_search，底层为 Google 级搜索。

        Args:
            query:         搜索查询词（3-5 个关键词最佳）
            plan_base_url: Coding Plan API 主机地址

        Returns:
            搜索结果列表 [{"title": "", "link": "", "snippet": "", "date": ""}, ...]
            失败返回空列表
        """
        client = await self._get_client()
        base = (plan_base_url or DEFAULT_PLAN_BASE_URL).rstrip("/")
        url = f"{base}/v1/coding_plan/search"

        try:
            resp = await client.post(url, json={"q": query})
            resp.raise_for_status()
            data = resp.json()

            base_resp = data.get("base_resp", {})
            if base_resp.get("status_code", 0) != 0:
                logger.error(
                    f"Search API error [{base_resp.get('status_code')}]: {base_resp.get('status_msg')}"
                )
                return []

            return data.get("organic", [])

        except Exception as e:
            logger.error(f"Search request failed for '{query}': {e}")
            return []

    async def search_batch(
        self,
        queries: list[str],
        plan_base_url: str | None = None,
        delay: float = 0.5,
    ) -> dict[str, list[dict]]:
        """
        批量搜索，串行执行（带间隔以避免限流）。

        Args:
            queries:       搜索查询词列表
            plan_base_url: Coding Plan API 主机地址
            delay:         每次搜索之间的间隔(秒)

        Returns:
            {query: [results]}
        """
        results: dict[str, list[dict]] = {}
        for q in queries:
            results[q] = await self.search(q, plan_base_url=plan_base_url)
            if delay > 0:
                await asyncio.sleep(delay)
        return results

    # ================================================================
    # Coding Plan: Image Understanding (图片理解)
    # ================================================================

    async def understand_image(
        self,
        prompt: str,
        image_url: str,
        plan_base_url: str | None = None,
    ) -> str:
        """
        调用 MiniMax Coding Plan 图片理解 API。

        端点: POST /v1/coding_plan/vlm

        Args:
            prompt:        对图片的提问或分析要求
            image_url:     图片 URL (http/https) 或本地路径，或 base64 data URL
            plan_base_url: Coding Plan API 主机地址

        Returns:
            模型对图片的分析文本，失败返回空字符串
        """
        client = await self._get_client()
        base = (plan_base_url or DEFAULT_PLAN_BASE_URL).rstrip("/")
        url = f"{base}/v1/coding_plan/vlm"

        # 如果是本地文件路径，转为 base64
        processed_url = image_url
        if not image_url.startswith(("http://", "https://", "data:")):
            import base64 as _b64
            try:
                with open(image_url, "rb") as f:
                    img_data = f.read()
                ext = os.path.splitext(image_url)[1].lower()
                fmt_map = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}
                fmt = fmt_map.get(ext, "jpeg")
                processed_url = f"data:image/{fmt};base64,{_b64.b64encode(img_data).decode('utf-8')}"
            except Exception as e:
                logger.error(f"Failed to read image file: {e}")
                return ""

        payload = {"prompt": prompt, "image_url": processed_url}

        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("content", "")

        except Exception as e:
            logger.error(f"Image understanding request failed: {e}")
            return ""


def _extract_json_block(text: str) -> str | None:
    """尝试从文本中提取 ```json ... ``` 代码块或 { ... } / [ ... ]"""
    import re

    # 1. ```json ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return m.group(1).strip()

    # 2. 直接找 JSON 对象或数组
    for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
        m = re.search(pattern, text)
        if m:
            return m.group(0).strip()

    return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度"""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        logger.warning(f"cosine_similarity: vector dimension mismatch ({len(a)} vs {len(b)}), truncating")
        min_len = min(len(a), len(b))
        a, b = a[:min_len], b[:min_len]
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
