"""GET /api/v1/ai/status"""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.services import ai_service

router = APIRouter(prefix="/api/v1")


class AIStatusResponse(BaseModel):
    qwen_reachable: bool           # 文档引擎
    qwen_busy: bool | None = None
    qwen_url: str
    qwen_chat_reachable: bool       # 聊天引擎
    qwen_chat_busy: bool | None = None
    qwen_chat_url: str
    message: str


@router.get("/ai/status", response_model=AIStatusResponse)
async def ai_status():
    return await ai_service.get_status()
