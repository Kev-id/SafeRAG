#copyright (C) 2025 Sophgo Technologies Inc.  All rights reserved.
#
# TPU-MLIR is licensed under the 2-Clause BSD License except for the
# third-party components.
#
# ==============================================================================
#
# FastAPI server wrapping the no-torch vision pipeline as an OpenAI-compatible
# chat API.  Supports both text-only and image (base64 data URI) input; the
# image path runs on the TPU ViT via pipeline_vision_light (no torch).
#
# Usage:
#   python server.py -m ../path/to/model.bmodel -c ../config --port 8000
#
# Dependencies: fastapi, uvicorn, pydantic, plus the vision_math stack.
# ==============================================================================

import asyncio
import time
import uuid
import sys
import os
import re
from threading import Lock
from typing import List, Optional, Union
import json

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline_vision_light import Qwen3_5


# ---------------------------------------------------------------------------
# Pydantic schemas (OpenAI-compatible; content may be text or a content array)
# ---------------------------------------------------------------------------

class ImageUrl(BaseModel):
    url: str  # local path or data URI ("data:image/png;base64,...")


class ContentItem(BaseModel):
    type: str = "text"          # "text" | "image_url"
    text: Optional[str] = None
    image_url: Optional[ImageUrl] = None


class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[ContentItem]] = ""


class ChatCompletionRequest(BaseModel):
    model: str = "tpu-qwen3.5"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# ---------------------------------------------------------------------------
# Streaming (SSE) schemas
# ---------------------------------------------------------------------------

class DeltaMessage(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage = Field(default_factory=DeltaMessage)
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[StreamChoice]


# ---------------------------------------------------------------------------
# Model management
# ---------------------------------------------------------------------------

_model: Optional[Qwen3_5] = None
_model_lock = Lock()


def get_model() -> Qwen3_5:
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not yet loaded")
    return _model


def _strip_thinking(text: str) -> str:
    """Remove  <think>...</think>  blocks from model output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _has_image(messages: List[dict]) -> bool:
    """True if any message content is a list carrying an image_url item."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "image_url" and isinstance(item.get("image_url"), dict):
                    return True
                if item.get("type") == "image" and isinstance(item.get("image"), str):
                    return True
    return False


# ---------------------------------------------------------------------------
# OpenAI-compatible inference
# ---------------------------------------------------------------------------

def run_chat(messages: List[ChatMessage], max_tokens: Optional[int] = None,
             clear_history: bool = False) -> str:
    """
    Stateless multi-turn inference (text or image).

    统一走 run_image（内部 _prefill 支持纯文本与图片、支持 clear_history 增量）。
    clear_history=True 清空 KV 从零开始（新会话）；False 则在现有 KV 上增量 prefill
    （同会话延续，图片 ViT 只算一次）。Thread-safe via the global lock.

    对话请求默认增量（False）；「切新会话」由独立的 POST /session/clear 清空 KV。
    """
    m = get_model()
    # exclude_none 是关键：去掉 text 项的 "image_url": None，否则 chat_template
    # 的 `'image_url' in item` 会把文本项误判成图，渲染出多余的 image_pad。
    msgs = [msg.model_dump(exclude_none=True) for msg in messages]

    try:
        with _model_lock:
            return m.run_image(msgs, max_tokens=max_tokens, clear_history=clear_history)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _stream_vision(msgs: List[dict], model_name: str, chat_id: str,
                   max_tokens: Optional[int], created: int, clear_history: bool = True):
    """SSE generator for image/text requests — streams stream_image() words."""
    m = get_model()

    first_chunk = ChatCompletionChunk(
        id=chat_id, created=created, model=model_name,
        choices=[StreamChoice(delta=DeltaMessage(role="assistant"))],
    )
    yield f"data: {first_chunk.model_dump_json()}\n\n"

    try:
        # Lock held for the whole stream (busy stays true while generating),
        # matching the text-only streaming path below.
        with _model_lock:
            for word in m.stream_image(msgs, max_tokens=max_tokens, clear_history=clear_history):
                chunk = ChatCompletionChunk(
                    id=chat_id, created=created, model=model_name,
                    choices=[StreamChoice(delta=DeltaMessage(content=word))],
                )
                yield f"data: {chunk.model_dump_json()}\n\n"
    except ValueError as e:
        err_chunk = ChatCompletionChunk(
            id=chat_id, created=created, model=model_name,
            choices=[StreamChoice(finish_reason="error")],
        )
        yield f"data: {err_chunk.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"
        return

    final_chunk = ChatCompletionChunk(
        id=chat_id, created=created, model=model_name,
        choices=[StreamChoice(delta=DeltaMessage(), finish_reason="stop")],
    )
    yield f"data: {final_chunk.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"


def run_chat_stream(messages: List[ChatMessage], model_name: str, chat_id: str,
                    max_tokens: Optional[int] = None, clear_history: bool = False):
    """SSE generator — yields "data: {...}\n\n" lines for streaming.

    统一走 _stream_vision（内部 stream_image 的 _prefill 支持纯文本与图片增量）。
    对话请求默认增量（False）；「切新会话」由独立的 POST /session/clear 清空 KV。
    """
    m = get_model()
    created = int(time.time())

    msgs = [msg.model_dump(exclude_none=True) for msg in messages]
    yield from _stream_vision(msgs, model_name, chat_id, max_tokens, created,
                              clear_history=clear_history)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="Qwen3.5 Vision API", version="1.1.0")


@app.get("/health")
async def health():
    """Health check. busy 表示引擎正在跑推理（模型锁被持有）。"""
    return {"status": "ok", "busy": _model_lock.locked()}


@app.post("/session/clear")
async def session_clear():
    """清空当前 KV session —— 前端切换到新会话 / 清空对话时调用。

    聊天请求默认在同一 KV 上增量续（图只首轮编码一次）；要开新会话
    必须先调本端点把旧 KV 清掉，否则旧会话上下文会污染新会话。
    """
    m = get_model()
    with _model_lock:
        m.model.clear_history()
        m.history_max_posid = 0
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# OpenAI-compatible model list  (needed by Cherry Studio / ChatBox / etc.)
# ---------------------------------------------------------------------------

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "sophgo"


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


@app.get("/v1/models")
async def list_models():
    return ModelList(data=[ModelInfo(id="tpu-qwen3.5")])


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint (streaming + non-streaming,
    text + image)."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    # 对话请求默认增量续 KV（run_chat/_stream_vision 的 clear_history 默认 False）；
    # 「切新会话/清空」由独立的 POST /session/clear 触发。
    if req.stream:
        return StreamingResponse(
            run_chat_stream(req.messages, req.model, chat_id, req.max_tokens),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    # run_chat 是 CPU/TPU 密集的同步函数：丢线程池执行，避免推理期间阻塞事件循环
    #（否则 /health 等其他请求会跟着卡住，ai_status 一查就超时）
    text = await asyncio.to_thread(run_chat, req.messages, req.max_tokens)
    return ChatCompletionResponse(
        id=chat_id,
        created=int(time.time()),
        model=req.model,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=text),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Qwen3.5 Text+Vision API Server")
    parser.add_argument("-m", "--model_path", type=str, required=True,
                        help="Path to the bmodel file")
    parser.add_argument("-c", "--config_path", type=str, default="../config",
                        help="Path to the processor config directory")
    parser.add_argument("-d", "--devid", type=int, default=0,
                        help="TPU device ID")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Bind address")
    parser.add_argument("--port", type=int, default=8000,
                        help="Listen port")
    parser.add_argument("--vision_t", type=int, default=2,
                        help="ViT patch feature temporal dim: 2 (1536-d, default) or 1 (768-d)")
    args = parser.parse_args()

    # Build a namespace compatible with Qwen3_5.__init__
    class ModelArgs:
        pass
    model_args = ModelArgs()
    model_args.devid = args.devid
    model_args.model_path = args.model_path
    model_args.config_path = args.config_path
    model_args.vision_t = args.vision_t

    print(f"Loading model from {args.model_path} ...")
    _model = Qwen3_5(model_args)
    print("Model loaded.")

    uvicorn.run(app, host=args.host, port=args.port)
