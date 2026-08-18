"""HTTP 客户端，封装对 Qwen 推理引擎的调用。"""

import time
import httpx
import logging

from backend.core.config import (
    QWEN_BASE_URL,
    QWEN_MODEL,
    QWEN_CONNECT_TIMEOUT,
    QWEN_READ_TIMEOUT,
    QWEN_MAX_TOKENS,
)

logger = logging.getLogger(__name__)


class QwenError(Exception):
    """Qwen 调用失败。"""
    pass


# 懒加载单例：复用连接池，避免每次调用都重建 TCP/HTTP 连接。
# 注意：单例长期复用，用完不能 close（进程退出时自然回收）。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """获取全局复用的 AsyncClient（第一次调用时创建）。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=QWEN_BASE_URL,
            timeout=httpx.Timeout(connect=QWEN_CONNECT_TIMEOUT, read=QWEN_READ_TIMEOUT),
        )
    return _client


async def check_health() -> dict:
    """检查 Qwen 推理引擎：返回 {"reachable": bool, "busy": bool | None}。

    reachable = 引擎能否响应 /health；
    busy = 引擎当前是否正在推理（unreachable 时为 None）。
    """
    try:
        resp = await _get_client().get("/health")
    except httpx.RequestError:
        return {"reachable": False, "busy": None}
    if resp.status_code != 200:
        return {"reachable": False, "busy": None}
    try:
        busy = bool(resp.json().get("busy", False))
    except Exception:
        busy = False
    return {"reachable": True, "busy": busy}


async def chat(messages: list[dict]) -> str:
    """
    发送对话给 Qwen，返回 assistant 的回复文本。

    Raises:
        QwenError: 连不上、超时或推理出错。
    """
    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "stream": False,
        "max_tokens": QWEN_MAX_TOKENS,  # 封顶单次生成长度，控制最坏耗时
    }

    logger.info("调用 Qwen，消息数=%d", len(messages))
    # 调试：打印实际发送给模型的完整 prompt，方便核对 RAG 检索到的法规有没有进去
    for i, m in enumerate(messages):
        logger.info("[发给模型] message[%d] role=%s:\n%s", i, m["role"], m["content"])

    start = time.monotonic()
    try:
        resp = await _get_client().post(
            "/v1/chat/completions",
            json=payload,
        )
    except httpx.ReadTimeout:
        raise QwenError(f"Qwen 推理超时（{QWEN_READ_TIMEOUT}s 内未返回）: 模型生成过慢或引擎卡住")
    except httpx.ConnectTimeout:
        raise QwenError("Qwen 连接超时: 引擎未启动或端口不通")
    except httpx.RequestError as e:
        raise QwenError(f"无法连接 Qwen 推理引擎: {e}")
    finally:
        logger.info("Qwen 调用耗时 %.1fs", time.monotonic() - start)

    if resp.status_code != 200:
        raise QwenError(f"Qwen 返回 HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise QwenError(f"Qwen 返回格式异常: {str(data)[:300]}")
