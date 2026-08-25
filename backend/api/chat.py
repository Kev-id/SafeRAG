"""流式聊天 API — SSE 代理（接入 RAG）。

POST /api/v1/chat/completions
  前端从"直连 Qwen :8000 SSE"改成"经 SafeRAG SSE 代理"：
  - 请求体带 enable_rag（默认关），开启时后端先检索法规注入 system 提示
  - 返回的 SSE 与 Qwen 原生格式完全一致（前端解析逻辑不用改）
"""

import asyncio
from typing import List, Optional, Union

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.core import qwen_client
from backend.services import chat_service

router = APIRouter(prefix="/api/v1")


class ImageUrl(BaseModel):
    url: str  # 本地路径或 data URI（"data:image/png;base64,..."）


class ContentItem(BaseModel):
    type: str = "text"          # "text" | "image_url"
    text: Optional[str] = None
    image_url: Optional[ImageUrl] = None


class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[ContentItem]] = ""


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    stream: bool = True          # 聊天走流式
    enable_rag: bool = False     # 默认关，前端明确开才检索


@router.post("/chat/completions")
async def chat_completions(req: ChatRequest):
    """流式聊天（SSE）。enable_rag=true 时先检索法规注入 system 提示。
    content 支持字符串或 [{type:text},{type:image_url}] 数组（图片 base64），
    RAG 检索只取其中的文本部分，图片项原样透传给 Qwen 引擎。"""
    if not req.stream:
        raise HTTPException(status_code=422, detail="暂只支持 stream=true")

    # model_dump 把 content 规整成 str 或 list[dict]，方便后端取文本 / 透传。
    # exclude_none=True 至关重要：去掉 text 项身上的 "image_url": None，否则
    # Qwen chat_template 用 `'image_url' in item` 判断会把纯文本项也当成图。
    messages = [m.model_dump(exclude_none=True) for m in req.messages]
    try:
        # build（检索+注入）在流开始前完成：抛错能正常转 HTTP 错误，
        # 不会漏成"200 + 残缺 SSE body"
        constructed, _sources = await asyncio.to_thread(
            chat_service.build_chat_messages, messages, req.enable_rag
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # 只有 Qwen 流式透传进生成器；连接失败会在这抛 QwenError → 500
    return StreamingResponse(
        qwen_client.chat_stream(constructed),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
