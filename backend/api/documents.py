"""POST /api/documents/process
GET  /api/documents/{id}
GET  /api/documents/{id}/download
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.services import document_service
from backend.repositories.document_repo import report_path

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# 请求 / 响应 Schema
# ---------------------------------------------------------------------------

class ProcessRequest(BaseModel):
    original_text: str = Field(..., min_length=1, max_length=1000)
    requirements: str = Field(..., min_length=1, max_length=1000)
    output_filename: str = Field(..., min_length=1, max_length=100)

class ProcessResponse(BaseModel):
    id: str
    status: str
    output_filename: str
    message: str

class DocumentDetail(BaseModel):
    id: str
    status: str
    output_filename: str
    original_text: str
    requirements: str
    report_content: str | None = None
    processing_note: str | None = None
    created_at: str
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.post("/documents/process", response_model=ProcessResponse, status_code=201)
async def process(req: ProcessRequest):
    try:
        doc = await document_service.process(
            original_text=req.original_text,
            requirements=req.requirements,
            output_filename=req.output_filename,
        )
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
        original_text=doc.original_text,
        requirements=doc.requirements,
        report_content=doc.report_content,
        processing_note=doc.processing_note,
        created_at=doc.created_at,
        completed_at=doc.completed_at,
    )


@router.get("/api/documents/{doc_id}/download")
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
