"""HTTP 客户端，封装对 Qwen 推理引擎的调用。"""

import httpx
import logging

from backend.config import QWEN_BASE_URL, QWEN_MODEL, QWEN_TIMEOUT

logger = logging.getLogger(__name__)


class QwenError(Exception):
    """Qwen 调用失败。"""
    pass


async def check_health() -> bool:
    """检查 Qwen 推理引擎是否可达。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3)) as client:
            resp = await client.get(f"{QWEN_BASE_URL}/health")
            return resp.status_code == 200
    except httpx.RequestError:
        return False


async def chat(messages: list[dict]) -> str:
    """
    发送对话给 Qwen，返回 assistant 的回复文本。

    Raises:
        QwenError: 连不上或推理出错。
    """
    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "stream": False,
    }

    logger.info("调用 Qwen，消息数=%d", len(messages))

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(QWEN_TIMEOUT)) as client:
            resp = await client.post(
                f"{QWEN_BASE_URL}/v1/chat/completions",
                json=payload,
            )
    except httpx.RequestError as e:
        raise QwenError(f"无法连接 Qwen 推理引擎: {e}")

    if resp.status_code != 200:
        raise QwenError(f"Qwen 返回 HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise QwenError(f"Qwen 返回格式异常: {str(data)[:300]}")
