"""认证 API — 公开（不需 JWT）。

POST /api/v1/auth/login   登录，返回 JWT access_token
GET  /api/v1/auth/me      返回当前登录用户信息（需 Bearer token）
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.services import auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


@router.post("/login", response_model=auth_service.LoginResponse, tags=["auth"])
async def login(req: LoginRequest):
    """登录，凭用户名/密码换发 JWT。"""
    return auth_service.login(req.username, req.password)


@router.get("/me", tags=["auth"])
async def me(user: dict = Depends(auth_service.get_current_user)):
    """返回当前登录用户信息。"""
    return {
        "username": user["username"],
        "role": user["role"],
        "full_name": user["full_name"],
    }