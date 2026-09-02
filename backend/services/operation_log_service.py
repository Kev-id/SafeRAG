"""操作日志业务层 — 写入吞错、查询分页。

record() 是审计写入的唯一入口：审计写入失败绝不影响业务主流程
（内部 catch Exception 后仅记 app logger）。
"""

import logging

from backend.repositories import operation_log_repo

logger = logging.getLogger(__name__)


def record(
    username: str,
    role: str,
    action: str,
    target: str = "",
    detail: str = "",
    ip: str = "",
    success: bool = True,
) -> None:
    """写一条操作日志；任何失败仅记日志，绝不向调用方抛错。"""
    try:
        operation_log_repo.add(
            ts=operation_log_repo.now_utc(),
            username=username,
            role=role,
            action=action,
            target=target,
            detail=detail,
            ip=ip,
            success=1 if success else 0,
        )
    except Exception:
        logger.error(
            "写入操作日志失败: action=%s username=%s target=%s",
            action, username, target,
            exc_info=True,
        )


def record_user(
    user: dict,
    action: str,
    target: str = "",
    detail: str = "",
    ip: str = "",
    success: bool = True,
) -> None:
    """从当前用户 dict（各路由绑定的 _user 依赖）写一条操作日志。"""
    record(
        username=user.get("username", ""),
        role=user.get("role", ""),
        action=action,
        target=target,
        detail=detail,
        ip=ip,
        success=success,
    )


def list_logs(
    page: int = 1,
    page_size: int = 50,
    username: str | None = None,
    action: str | None = None,
    success: int | None = None,
) -> dict:
    """分页查询操作日志，返回 {"total", "page", "page_size", "items"}。"""
    if page < 1:
        page = 1
    if not 1 <= page_size <= 200:
        page_size = 50
    rows, total = operation_log_repo.list_logs(
        offset=(page - 1) * page_size,
        limit=page_size,
        username=username or None,
        action=action or None,
        success=success,
    )
    return {"total": total, "page": page, "page_size": page_size, "items": rows}