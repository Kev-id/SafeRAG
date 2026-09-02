"""流式聊天 API — SSE 代理（接入 RAG）。

POST /api/v1/chat/completions
  前端从"直连 Qwen :8000 SSE"改成"经 SafeRAG SSE 代理"：
  - 请求体带 enable_rag（默认关），开启时后端先检索法规注入 system 提示
  - 返回的 SSE 与 Qwen 原生格式完全一致（前端解析逻辑不用改）
"""

import asyncio
import json
import time
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.core import qwen_client
from backend.services import chat_service
from backend.services.auth_service import perm_user_sys_sec

router = APIRouter(prefix="/api/v1")


def _materials_chunk(sources: list[str]) -> str:
    """构造最后一条带 rag_materials 的 OpenAI 风格 chunk（delta 为空，材料不进正文）。

    前端读完即可用 sources（每条 "[编号] 来源 文本…"）做折叠展示：缩略显示来源，
    点开看全文。取到内容后自行渲染，不要并进 assistant 消息存历史。
    """
    payload = {
        "id": "chatcmpl-saferag-" + str(int(time.time() * 1000)),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": qwen_client.QWEN_CHAT_MODEL,
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}],
        "rag_materials": sources,
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _stream_with_materials(inner, sources: list[str] | None):
    """在 [DONE] 前追加一条带 rag_materials 的 chunk；无材料则原样透传。"""
    async for line in inner:
        if line.strip() == "data: [DONE]":
            if sources:
                yield _materials_chunk(sources)
            yield line
            return
        yield line


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
    region: str | None = None    # 废弃兼容：省（旧），新用 provinces
    provinces: list[str] = []    # 可选：多选省，透传给检索器做地域过滤
    cities: list[str] = []       # 可选：多选市，透传给检索器做地域过滤
    file_types: list[str] = []   # 必选语义：多选文件类型（国家法律/行政法规/地方法规等）。空列表 = 什么都不选 → 不注入法规


@router.post("/chat/completions")
async def chat_completions(req: ChatRequest, _user: dict = Depends(perm_user_sys_sec)):
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
        constructed, sources = await asyncio.to_thread(
            chat_service.build_chat_messages,
            messages, req.enable_rag,
            req.provinces, req.cities, req.file_types
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # RAG 开启且检索到材料 → 流末尾单独下发 rag_materials（不进 assistant 正文）
    stream = _stream_with_materials(
        qwen_client.chat_stream(constructed),
        sources if (req.enable_rag and sources) else None,
    )

    # 连接失败会在这抛 QwenError → 500
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
