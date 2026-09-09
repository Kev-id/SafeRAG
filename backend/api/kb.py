"""知识库文件 API

POST   /api/v1/files               上传知识库文件（multipart/form-data）
GET    /api/v1/files?               列出知识库文件
GET    /api/v1/files/{filename}    获取单个文件详情
DELETE /api/v1/files/{filename}    删除知识库文件
"""

from typing import Optional
import os
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel


from backend.services import knowledge_service, operation_log_service
from backend.repositories import kb_file_repo
from backend.services.auth_service import (
    ROLE_SEC,
    perm_user_sys_sec_aud, perm_user_sys_sec, perm_sys_sec_aud, perm_sec,
)

router = APIRouter(prefix="/api/v1", tags=["knowledge"])

# 敏感文件仅授权给 安全保密员 可读可下载；其余角色(含系统管理员/普通用户/审计员)不可见/不可下
_SENSITIVE_READABLE = {ROLE_SEC}


def _can_read_sensitive(role: str) -> bool:
    return role in _SENSITIVE_READABLE


class KbUploadResponse(BaseModel):
    message: str


class KbFileItem(BaseModel):
    """列表项 — 只含元数据，不含正文。"""
    filename: str
    md5: str | None = None
    file_type: str | None = None
    region: str | None = None
    city: str | None = None
    size: int | None = None
    chunk_count: int = 0
    status: str = "building"
    message: str | None = None
    sensitive: bool = False
    updated_at: str | None = None


class KbFileDetail(KbFileItem):
    """详情 — 列表项 + 正文内容。"""
    content: str | None = None

class KbStatsResponse(BaseModel):
    file_count: int
    chunk_count: int
    total_size: int
    status_counts: dict[str, int]

class SensitiveUpdate(BaseModel):
    sensitive: bool


@router.post("/files", response_model=KbUploadResponse, status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    file_type: Optional[str] = Form(None),
    region: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    _user: dict = Depends(perm_user_sys_sec),
    ):
    """上传文件，登记到 SQLite 并写入 ChromaDB 索引。region/city 由前端传入。"""
    content = await file.read()
    try:
        result = await knowledge_service.upload_kb_file(
            file.filename or "", content, file_type=file_type, region=region, city=city
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(
        _user, "upload_kb_file", target=file.filename or "",
        detail=f"file_type={file_type or ''}/size={len(content)}B", ip=ip,
    )
    return result


@router.get("/files", response_model=list[KbFileItem])
async def list_files(file_type:Optional[str]=None, status: Optional[str]=None, keyword: Optional[str]=None, region: Optional[str]=None, city: Optional[str]=None, _user: dict = Depends(perm_user_sys_sec_aud)):
    """列出知识库文件（读登记册，权威源）。"""
    items = await knowledge_service.list_kb_files(file_type=file_type, status=status, keyword=keyword, region=region, city=city)
    # 敏感文件对非授权角色（普通用户/审计员/系统管理员）完全隐藏
    if not _can_read_sensitive(_user["role"]):
        items = [i for i in items if not i.get("sensitive")]
    return [KbFileItem(**item) for item in items]


@router.get("/files/{filename}", response_model=KbFileDetail)
async def get_file(filename: str, _user: dict = Depends(perm_user_sys_sec_aud)):
    """获取单个文件详情（元数据 + 正文）。敏感文件对非授权角色返回 403。"""
    try:
        item = await knowledge_service.get_kb_file(filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if item.get("sensitive") and not _can_read_sensitive(_user["role"]):
        raise HTTPException(status_code=403, detail="无权限访问敏感文件")
    return KbFileDetail(**item)


@router.delete("/files/{filename}", response_model=KbUploadResponse)
async def delete_file(filename: str, request: Request, _user: dict = Depends(perm_user_sys_sec)):
    """删除知识库文件：索引 + 磁盘 + 登记册。

    普通/系统管理员可删除常规文件（所见即所得）；敏感文件仅安全保密员可删。"""
    safe_name = os.path.basename(filename)
    item = kb_file_repo.get(safe_name)
    if item is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if item.get("sensitive") and not _can_read_sensitive(_user["role"]):
        raise HTTPException(status_code=403, detail="无权限删除敏感文件")
    try:
        result = await knowledge_service.delete_kb_file(safe_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(_user, "delete_kb_file", target=safe_name, ip=ip)
    return result


@router.patch("/files/{filename}/sensitive", response_model=KbUploadResponse)
async def set_sensitive(filename: str, body: SensitiveUpdate, request: Request, _user: dict = Depends(perm_sec)):
    """标记/撤销敏感文件（仅安全保密员）。"""
    item = kb_file_repo.get(filename)
    if item is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not kb_file_repo.set_sensitive(filename, body.sensitive):
        raise HTTPException(status_code=404, detail="文件不存在")
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(
        _user, "set_sensitive", target=filename,
        detail=f"sensitive={body.sensitive}", ip=ip,
    )
    return KbUploadResponse(
        message="已标记为敏感文件" if body.sensitive else "已撤销敏感标记"
    )


@router.get("/kb/stats", response_model=KbStatsResponse)
async def get_kb_stats(_user: dict = Depends(perm_sys_sec_aud)):
    """获取知识库统计信息（后台监控用：系统/安全/审计三员可见，普通用户不可见）"""
    return knowledge_service.get_stats()
