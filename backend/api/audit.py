"""审计日志 API — 仅审计管理员可查（三权分立中审计是审计专员的独占职责）。

GET /api/v1/audit/logs   分页查询操作日志，支持按用户名/动作/成败过滤
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.services import operation_log_service
from backend.services.auth_service import audadmin_only

router = APIRouter(prefix="/api/v1")


class OperationLogItem(BaseModel):
    id: int
    ts: str          # 操作时间（UTC）
    username: str    # 操作者
    role: str        # 操作时角色快照
    action: str      # 动作类型：login / create_document / ...
    target: str = ""      # 目标对象：doc_id / filename / username
    detail: str = ""      # 补充说明：任务类型 / 失败原因等
    ip: str = ""          # 客户端 IP
    success: int = 1      # 1 成功 / 0 失败


class LogListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[OperationLogItem]


@router.get("/audit/logs", response_model=LogListResponse)
async def list_logs(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    username: str | None = Query(None, description="按操作者过滤"),
    action: str | None = Query(None, description="按动作类型过滤，如 login/create_document"),
    success: int | None = Query(None, ge=0, le=1, description="按结果过滤: 1 成功 / 0 失败"),
    _user: dict = Depends(audadmin_only),
):
    """分页查询操作日志（仅审计管理员可访问，其它角色返回 403）。"""
    return operation_log_service.list_logs(
        page=page,
        page_size=page_size,
        username=username,
        action=action,
        success=success,
    )