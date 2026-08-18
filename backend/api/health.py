from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1")
from pydantic import BaseModel
from backend.services import health_service
class HealthResponse(BaseModel):
    status: str
    sqlite: bool
    qwen: bool
    chroma: bool

@router.get("/health", response_model=HealthResponse)
async def health_check():
    return await health_service.get_health_status()
