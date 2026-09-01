from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.services import health_service
from backend.services.auth_service import any_role

router = APIRouter(prefix="/api/v1")


class HealthResponse(BaseModel):
    status: str
    sqlite: bool
    qwen: bool          # 文档处理引擎
    qwen_chat: bool     # 流式对话引擎
    chroma: bool
    retriever_loading: bool = False   # 检索器（BM25）是否还在后台预热


@router.get("/health", response_model=HealthResponse)
async def health_check(_user: dict = Depends(any_role)):
    return await health_service.get_health_status()
