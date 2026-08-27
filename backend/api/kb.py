"""知识库文件 API

POST   /api/v1/files               上传知识库文件（multipart/form-data）
GET    /api/v1/files?               列出知识库文件
GET    /api/v1/files/{filename}    获取单个文件详情
DELETE /api/v1/files/{filename}    删除知识库文件
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel


from backend.services import knowledge_service

router = APIRouter(prefix="/api/v1")


class KbUploadResponse(BaseModel):
    message: str


class KbFileItem(BaseModel):
    """列表项 — 只含元数据，不含正文。"""
    filename: str
    md5: str | None = None
    file_type: str | None = None
    region: str | None = None
    size: int | None = None
    chunk_count: int = 0
    status: str = "building"
    message: str | None = None
    updated_at: str | None = None


class KbFileDetail(KbFileItem):
    """详情 — 列表项 + 正文内容。"""
    content: str | None = None

class KbStatsResponse(BaseModel):
    file_count: int
    chunk_count: int
    total_size: int
    status_counts: dict[str, int]

@router.post("/files", response_model=KbUploadResponse, status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    file_type: str = Form(...),
    region: Optional[str] = Form(None),
    ):
    """上传 txt 文件，登记到 SQLite 并写入 ChromaDB 索引。"""
    content = await file.read()
    try:
        return await knowledge_service.upload_kb_file(file.filename or "", content, file_type=file_type, region=region)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/files", response_model=list[KbFileItem])
async def list_files(file_type:Optional[str]=None, status: Optional[str]=None, keyword: Optional[str]=None, region: Optional[str]=None):
    """列出知识库文件（读登记册，权威源）。"""
    items = await knowledge_service.list_kb_files(file_type=file_type, status=status, keyword=keyword, region=region)
    return [KbFileItem(**item) for item in items]


@router.get("/files/{filename}", response_model=KbFileDetail)
async def get_file(filename: str):
    """获取单个文件详情（元数据 + 正文）。"""
    try:
        item = await knowledge_service.get_kb_file(filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return KbFileDetail(**item)


@router.delete("/files/{filename}", response_model=KbUploadResponse)
async def delete_file(filename: str):
    """删除知识库文件：索引 + 磁盘 + 登记册。"""
    try:
        return await knowledge_service.delete_kb_file(filename)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/kb/stats", response_model=KbStatsResponse)
async def get_kb_stats():
    """获取知识库统计信息"""
    return knowledge_service.get_stats()
