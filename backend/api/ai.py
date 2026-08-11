"""GET /api/ai/status"""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.services import ai_service

router = APIRouter()


class AIStatusResponse(BaseModel):
    qwen_reachable: bool
    qwen_url: str
    message: str


@router.get("/api/ai/status", response_model=AIStatusResponse)
async def ai_status():
    return await ai_service.get_status()
