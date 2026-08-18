from fastapi import APIRouter
from pydantic import BaseModel

from backend.services import health_service

router = APIRouter(prefix="/api/v1")


class HealthResponse(BaseModel):
    status: str
    sqlite: bool
    qwen: bool
    chroma: bool


@router.get("/health", response_model=HealthResponse)
async def health_check():
    return await health_service.get_health_status()
