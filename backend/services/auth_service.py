"""认证服务：登录签发 JWT、取当前用户、角色校验依赖。

供各业务路由声明依赖使用：
  from backend.services.auth_service import any_role, sysadmin_only, secadmin_only, ops_and_sec
"""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from backend.core import security
from backend.repositories import user_repo
from backend.services import operation_log_service

# 角色常量
ROLE_SYS = "sysadmin"
ROLE_SEC = "secadmin"
ROLE_AUD = "audadmin"

# Bearer token 提取（auto_error=False 便于区分"未携带"和"无效"）
bearer_scheme = HTTPBearer(auto_error=False)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    full_name: str


def login(username: str, password: str, ip: str = "") -> LoginResponse:
    """校验用户名密码，成功返回带 JWT 的响应；失败抛 401。登录成败均写入操作日志。"""
    user = user_repo.get_user_by_username(username)
    if not user or not user["is_active"]:
        operation_log_service.record(
            username, "", "login", detail="用户名或密码错误", ip=ip, success=False
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not security.verify_password(password, user["password_hash"]):
        operation_log_service.record(
            username, user["role"], "login", detail="用户名或密码错误", ip=ip, success=False
        )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = security.create_access_token(user["username"], user["role"])
    operation_log_service.record(user["username"], user["role"], "login", ip=ip, success=True)
    return LoginResponse(
        access_token=token,
        username=user["username"],
        role=user["role"],
        full_name=user["full_name"],
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """校验 JWT 并回读库确认账号有效，返回用户 dict。

    Token 来源优先级：Authorization: Bearer 请求头 > 查询参数 ?token=。
    查询参数用于纯 <a href>/window.open 类 GET（文件/文档下载），无法携带请求头。
    """
    token = None
    if credentials is not None:
        token = credentials.credentials
    else:
        token = request.query_params.get("token")
    if not token:
        raise _unauthorized("未提供认证凭据")
    payload = security.decode_token(token)
    if payload is None:
        raise _unauthorized("令牌无效或已过期")
    username = payload.get("sub")
    user = user_repo.get_user_by_username(username) if username else None
    if not user or not user["is_active"]:
        raise _unauthorized("账号不存在或已停用")
    return user


def require_roles(*allowed_roles: str):
    """依赖工厂：当前用户角色必须在 allowed_roles 内，否则抛 403。"""

    def dependency(current: dict = Depends(get_current_user)) -> dict:
        if current["role"] not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"权限不足：需要角色 {'/'.join(allowed_roles)}",
            )
        return current

    return dependency


# 预定义角色依赖（按确认的权限矩阵）
any_role = require_roles(ROLE_SYS, ROLE_SEC, ROLE_AUD)   # 登录即读
sysadmin_only = require_roles(ROLE_SYS)                  # 业务文档编排/删除
secadmin_only = require_roles(ROLE_SEC)                  # 知识库安全数据写
ops_and_sec = require_roles(ROLE_SYS, ROLE_SEC)          # AI 对话
audadmin_only = require_roles(ROLE_AUD)                  # 审计日志专查