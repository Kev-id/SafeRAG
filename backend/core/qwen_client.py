"""HTTP 客户端，封装对 Qwen 推理引擎的调用。"""

import time
import httpx
import logging

from backend.core.config import (
    QWEN_BASE_URLS,
    QWEN_MODEL,
    QWEN_CONNECT_TIMEOUT,
    QWEN_READ_TIMEOUT,
    QWEN_MAX_TOKENS,
)

logger = logging.getLogger(__name__)


class QwenError(Exception):
    """Qwen 调用失败。"""
    pass


# 引擎池：每 URL 一个 AsyncClient（httpx 的 base_url 是 client 级不可变的，必须分开）。
# 懒加载 + 长期复用，用完不能 close（进程退出时自然回收）。
_engines: dict[str, httpx.AsyncClient] = {}
# 轮询游标：asyncio 单线程内"读索引→自增→返回"之间没有 await，相对其它协程是原子的，
# 所以多个 worker 并发选引擎不会撞到同一台。
_rr_index = 0


def _get_client(url: str) -> httpx.AsyncClient:
    """按引擎 URL 取（懒建）对应的 AsyncClient。"""
    client = _engines.get(url)
    if client is None:
        client = httpx.AsyncClient(
            base_url=url,
            timeout=httpx.Timeout(QWEN_CONNECT_TIMEOUT, read=QWEN_READ_TIMEOUT),
        )
        _engines[url] = client
    return client


def _next_engine() -> tuple[int, str]:
    """轮询选下一个引擎，返回 (索引, url)。

    同步执行、无 await，单线程下原子安全。每文档只调一次 chat，
    所以引擎在任务开始时一次性选定，整个文档处理固定在这台引擎上。
    """
    global _rr_index
    idx = _rr_index % len(QWEN_BASE_URLS)
    _rr_index += 1
    return idx, QWEN_BASE_URLS[idx]


async def check_engines() -> list[dict]:
    """逐个健康检查所有引擎。每项: {"url", "reachable", "busy"}。

    reachable = 该引擎能否响应 /health；
    busy = 该引擎当前是否正在推理（unreachable 时为 None）。
    """
    results = []
    for url in QWEN_BASE_URLS:
        try:
            resp = await _get_client(url).get("/health")
        except httpx.RequestError:
            results.append({"url": url, "reachable": False, "busy": None})
            continue
        if resp.status_code != 200:
            results.append({"url": url, "reachable": False, "busy": None})
            continue
        try:
            busy = bool(resp.json().get("busy", False))
        except Exception:
            busy = False
        results.append({"url": url, "reachable": True, "busy": busy})
    return results


async def chat(messages: list[dict]) -> str:
    """
    轮询选一台引擎发送对话给 Qwen，返回 assistant 的回复文本。

    Raises:
        QwenError: 连不上、超时或推理出错。
    """
    engine_idx, url = _next_engine()
    payload = {
        "model": QWEN_MODEL,
        "messages": messages,
        "stream": False,
        "max_tokens": QWEN_MAX_TOKENS,  # 封顶单次生成长度，控制最坏耗时
    }

    logger.info("调用 Qwen 引擎[%d] %s，消息数=%d", engine_idx, url, len(messages))
    # 调试：完整 prompt 降为 DEBUG（多引擎高频任务下 INFO 会刷屏），核对 RAG 法规有没有进去
    for i, m in enumerate(messages):
        logger.debug("[发给模型] message[%d] role=%s:\n%s", i, m["role"], m["content"])

    start = time.monotonic()
    try:
        resp = await _get_client(url).post(
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
        logger.info("Qwen[%d] 调用耗时 %.1fs", engine_idx, time.monotonic() - start)

    if resp.status_code != 200:
        raise QwenError(f"Qwen 返回 HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise QwenError(f"Qwen 返回格式异常: {str(data)[:300]}")
