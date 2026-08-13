"""POST   /api/v1/documents/process
GET    /api/v1/documents
GET    /api/v1/documents/{id}
GET    /api/v1/documents/{id}/download
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.services import document_service
from backend.repositories.document_repo import report_path, DocStatus

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# 请求 / 响应 Schema
# ---------------------------------------------------------------------------

class ProcessRequest(BaseModel):
    task_type: str = Field(..., min_length=1, max_length=50)
    original_text: str = Field(..., min_length=1, max_length=1000)
    requirements: str = Field(..., min_length=1, max_length=1000)
    output_filename: str = Field(..., min_length=1, max_length=100)

class ProcessResponse(BaseModel):
    id: str
    status: str
    output_filename: str
    message: str

class DocumentListItem(BaseModel):
    """列表项 — 只含概要字段，不含正文。"""
    id: str
    status: str
    output_filename: str
    task_type: str
    created_at: str
    completed_at: str | None = None

class DocumentListResponse(BaseModel):
    items: list[DocumentListItem]
    total: int
    page: int
    page_size: int

class DocumentDetail(BaseModel):
    id: str
    status: str
    output_filename: str
    task_type: str
    original_text: str
    requirements: str
    report_content: str | None = None
    created_at: str
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    status: str | None = Query(None, description="按状态过滤: pending/processing/completed/failed"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
):
    """分页获取文档列表，按创建时间倒序。"""
    # 把字符串转成 DocStatus 枚举
    status_enum: DocStatus | None = None
    if status is not None:
        try:
            status_enum = DocStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"无效的状态值: {status}，允许: pending/processing/completed/failed",
            )

    result = await document_service.list_documents(
        status=status_enum,
        page=page,
        page_size=page_size,
    )

    # 转成 API schema
    return DocumentListResponse(
        items=[
            DocumentListItem(
                id=doc.id,
                status=doc.status.value,
                output_filename=doc.output_filename,
                task_type=doc.task_type,
                created_at=doc.created_at,
                completed_at=doc.completed_at,
            )
            for doc in result["items"]
        ],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )

@router.post("/documents/process", response_model=ProcessResponse, status_code=201)
async def process(req: ProcessRequest):
    try:
        doc = await document_service.process(
            task_type=req.task_type,
            original_text=req.original_text,
            requirements=req.requirements,
            output_filename=req.output_filename,
        )
    except KeyError:
        raise HTTPException(status_code=422, detail=f"无效的任务类型: {req.task_type}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return ProcessResponse(
        id=doc.id,
        status=doc.status.value,
        output_filename=doc.report_filename,
        message="处理完成",
    )


@router.get("/documents/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: str):
    try:
        doc = await document_service.get_detail(doc_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return DocumentDetail(
        id=doc.id,
        status=doc.status.value,
        output_filename=doc.report_filename,
        task_type=doc.task_type,
        original_text=doc.original_text,
        requirements=doc.requirements,
        report_content=doc.report_content,
        created_at=doc.created_at,
        completed_at=doc.completed_at,
    )


@router.get("/documents/{doc_id}/download")
async def download(doc_id: str):
    try:
        doc = await document_service.get_detail(doc_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    path = report_path(doc)
    return FileResponse(
        path=path,
        filename=doc.report_filename,
        media_type="text/markdown; charset=utf-8",
    )
