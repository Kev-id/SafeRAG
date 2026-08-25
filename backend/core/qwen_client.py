"""HTTP 客户端，封装对 Qwen 推理引擎的调用。"""

import os
import time
from typing import AsyncIterator

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


def _summarize_content(content) -> str:
    """日志用：把消息内容压成一行摘要，绝不打印大段文本 / base64。

    普通文本 → text(N字): 前80字…
    多模态数组 → text(N字) + image_url…
    """
    if isinstance(content, str):
        text = content.strip()
        if len(text) > 80:
            return f"text({len(text)}字): {text[:80]}…"
        return f"text({len(text)}字): {text}" if text else "text(0字)"
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(type(item).__name__)
                continue
            t = item.get("type")
            if t == "text":
                txt = (item.get("text") or "").strip()
                parts.append(f"text({len(txt)}字)")
            elif t == "image_url":
                parts.append("image_url")
            else:
                parts.append(str(t))
        return " + ".join(parts) if parts else "empty"
    return type(content).__name__


# 按用途隔离的两个引擎（文档处理 / 流式对话）：
# 环境变量覆盖，默认回退 QWEN_BASE_URL / QWEN_MODEL（兼容只配单引擎的旧部署）。
# 用 os.getenv 而不是 from config import QWEN_DOC_URL —— config.py 可能因本机
# 跳过跟踪而缺这些新字段，直接 import 会在盒子上 AttributeError。
QWEN_DOC_URL = os.getenv("QWEN_DOC_URL", QWEN_BASE_URL)
QWEN_CHAT_URL = os.getenv("QWEN_CHAT_URL", QWEN_BASE_URL)
QWEN_DOC_MODEL = os.getenv("QWEN_DOC_MODEL", QWEN_MODEL)
QWEN_CHAT_MODEL = os.getenv("QWEN_CHAT_MODEL", QWEN_MODEL)


def _make_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(QWEN_CONNECT_TIMEOUT, read=QWEN_READ_TIMEOUT),
    )


# 懒加载单例：复用连接池，避免每次调用都重建 TCP/HTTP 连接。
# 注意：单例长期复用，用完不能 close（进程退出时自然回收）。
_doc_client: httpx.AsyncClient | None = None
_chat_client: httpx.AsyncClient | None = None


def _get_doc_client() -> httpx.AsyncClient:
    """文档处理引擎的连接（非流式 + 健康检查）。"""
    global _doc_client
    if _doc_client is None:
        _doc_client = _make_client(QWEN_DOC_URL)
    return _doc_client


def _get_chat_client() -> httpx.AsyncClient:
    """流式对话引擎的连接。"""
    global _chat_client
    if _chat_client is None:
        _chat_client = _make_client(QWEN_CHAT_URL)
    return _chat_client


async def _health_of(client: httpx.AsyncClient) -> dict:
    """探测单个引擎：返回 {"reachable": bool, "busy": bool | None}。

    reachable = 引擎能否响应 /health；
    busy = 引擎当前是否正在推理（unreachable 时为 None）。
    """
    try:
        resp = await client.get("/health")
    except httpx.RequestError:
        return {"reachable": False, "busy": None}
    if resp.status_code != 200:
        return {"reachable": False, "busy": None}
    try:
        busy = bool(resp.json().get("busy", False))
    except Exception:
        busy = False
    return {"reachable": True, "busy": busy}


async def check_health() -> dict:
    """检查文档处理引擎（QWEN_DOC_URL）—— 兼容旧接口，前端未迁移也能用。"""
    return await _health_of(_get_doc_client())


async def check_chat_health() -> dict:
    """检查流式对话引擎（QWEN_CHAT_URL）。"""
    return await _health_of(_get_chat_client())


async def chat(messages: list[dict]) -> str:
    """
    发送对话给 Qwen，返回 assistant 的回复文本。

    Raises:
        QwenError: 连不上、超时或推理出错。
    """
    payload = {
        "model": QWEN_DOC_MODEL,
        "messages": messages,
        "stream": False,
        "max_tokens": QWEN_MAX_TOKENS,  # 封顶单次生成长度，控制最坏耗时
    }

    logger.info("调用 Qwen，消息数=%d", len(messages))
    # 只打每条消息一行摘要（长度/类型），不打印完整内容（可能含 base64）
    for i, m in enumerate(messages):
        logger.info("[发给模型] message[%d] role=%s: %s", i, m["role"], _summarize_content(m.get("content")))

    start = time.monotonic()
    try:
        resp = await _get_doc_client().post(
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


async def chat_stream(messages: list[dict]) -> AsyncIterator[str]:
    """流式对话：逐块透传 Qwen 的 SSE 原始行（`data: {...}\n\n`）。

    转发策略：不改 Qwen 的 chunk，原样 yield 每一行（含 `data: [DONE]`），
    前端拿到的是和直连 Qwen 完全一致的 OpenAI 兼容 SSE，解析逻辑不用改。

    错误：连接失败/超时/非 200 → 抛 QwenError（在流未开始前由上层转 503/500；
    流开始后中断 → 由调用方决定怎么收尾）。
    """
    payload = {
        "model": QWEN_CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "max_tokens": QWEN_MAX_TOKENS,
    }

    logger.info("调用 Qwen（流式），消息数=%d", len(messages))
    # 只打每条消息一行摘要（长度/类型），不打印完整内容（可能含 base64）
    for i, m in enumerate(messages):
        logger.info("[发给模型] message[%d] role=%s: %s", i, m["role"], _summarize_content(m.get("content")))

    start = time.monotonic()
    try:
        async with _get_chat_client().stream(
            "POST", "/v1/chat/completions", json=payload
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread())[:300]
                raise QwenError(f"Qwen 流式返回 HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue  # 跳过空行 / keep-alive / 非 data 行
                yield line + "\n\n"  # 补回 SSE 空行分隔，原样透传
                if line.strip() == "data: [DONE]":
                    break
    except httpx.ReadTimeout:
        raise QwenError(f"Qwen 流式读超时（{QWEN_READ_TIMEOUT}s）：生成中段断连")
    except httpx.ConnectTimeout:
        raise QwenError("Qwen 连接超时: 引擎未启动或端口不通")
    except httpx.RequestError as e:
        raise QwenError(f"无法连接 Qwen 推理引擎: {e}")
    finally:
        logger.info("Qwen 流式调用结束，耗时 %.1fs", time.monotonic() - start)
