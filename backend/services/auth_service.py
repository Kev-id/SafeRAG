"""认证服务：登录签发 JWT、取当前用户、角色校验依赖。

供各业务路由声明依赖使用，名称直接枚举放行的角色（user/sys/sec/aud）：
  from backend.services.auth_service import perm_user_sys_sec_aud, perm_user_sys_sec, perm_sys_sec_aud, perm_sec, perm_sys
"""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from backend.core import security
from backend.repositories import user_repo

# 角色常量
ROLE_USER = "user"       # 普通用户
ROLE_SYS = "sysadmin"    # 系统管理员
ROLE_SEC = "secadmin"    # 安全保密员
ROLE_AUD = "audadmin"    # 审计员

# Bearer token 提取（auto_error=False 便于区分"未携带"和"无效"）
bearer_scheme = HTTPBearer(auto_error=False)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    full_name: str


def login(username: str, password: str) -> LoginResponse:
    """校验用户名密码，成功返回带 JWT 的响应；失败抛 401。"""
    user = user_repo.get_user_by_username(username)
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not security.verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = security.create_access_token(user["username"], user["role"])
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


# 预定义角色依赖（按权限矩阵：命名直接枚举放行的角色 user/sys/sec/aud）
#
#   user       普通用户：业务全功能（读写），禁监控/用户管理/系统配置/审计
#   sysadmin   系统管理员：业务 + 用户管理 + 系统配置 + 后台监控
#   secadmin   安全保密员：业务（含知识库写） + 后台监控；专属=知识库文件删除
#   audadmin   审计员：全局只读（审计日志未来接入）
perm_user_sys_sec_aud = require_roles(ROLE_USER, ROLE_SYS, ROLE_SEC, ROLE_AUD)  # 登录即可读
perm_user_sys_sec     = require_roles(ROLE_USER, ROLE_SYS, ROLE_SEC)            # 业务操作（建/删报告、对话、知识库上传）
perm_sys_sec_aud      = require_roles(ROLE_SYS, ROLE_SEC, ROLE_AUD)             # 后台监控
perm_sec              = require_roles(ROLE_SEC)                                 # 安全保密员专属（知识库删除、敏感标记）
perm_sys              = require_roles(ROLE_SYS)                                 # 系统管理员专属（用户管理）