"""用户管理 API —— 仅系统管理员（sysadmin）可调用。

POST   /api/v1/users                    创建用户（普通用户/三员）
GET    /api/v1/users                    用户列表
PATCH  /api/v1/users/{user_id}          更新（姓名/角色/启停）
POST   /api/v1/users/{user_id}/reset-password   重置密码
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.repositories import user_repo
from backend.services.auth_service import get_current_user, user_admin

router = APIRouter(prefix="/api/v1/users", tags=["users"])

VALID_ROLES = frozenset(user_repo.ROLES.keys())


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, description="登录用户名")
    full_name: str = Field(default="", max_length=64, description="姓名")
    role: str = Field(default="user", description="角色，默认普通用户")
    password: str = Field(min_length=8, max_length=128, description="初始密码（至少 8 位）")


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=64)
    role: str | None = None
    is_active: bool | None = None


class ResetPassword(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


@router.get("")
def list_users(_admin: dict = Depends(user_admin)):
    return user_repo.list_users()


@router.post("")
def create_user(body: UserCreate, admin: dict = Depends(user_admin)):
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"无效角色：{body.role}")
    user = user_repo.create_user(
        username=body.username.strip(),
        full_name=body.full_name.strip(),
        role=body.role,
        password=body.password,
    )
    if user is None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    return user


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserUpdate, admin: dict = Depends(user_admin)):
    if body.role is not None and body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"无效角色：{body.role}")
    # 防止系统管理员停用/改角色自己造成锁死
    if admin["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能停用或修改自己的账号")
    user = user_repo.update_user(
        user_id,
        full_name=body.full_name,
        role=body.role,
        is_active=body.is_active,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


@router.post("/{user_id}/reset-password")
def reset_password(user_id: int, body: ResetPassword, _admin: dict = Depends(user_admin)):
    if not user_repo.reset_password(user_id, body.new_password):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "message": "密码已重置"}