"""POST   /api/v1/documents/process
GET    /api/v1/documents
GET    /api/v1/documents/{id}
GET    /api/v1/documents/{id}/download
PATCH  /api/v1/documents/{id}/sections/{index}             手动改某一节
POST   /api/v1/documents/{id}/sections/{index}/revise      追加要求/材料重生成某一节
"""
import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from backend.core import doc_exporter
from backend.repositories.document_repo import DocStatus, report_path
from backend.repositories.template_repo import section_no
from backend.services import document_service, operation_log_service, template_service
from backend.services.auth_service import perm_sys_sec_aud, perm_user_sys_sec, perm_user_sys_sec_aud

router = APIRouter(prefix="/api/v1", tags=["documents"])


# ---------------------------------------------------------------------------
# 请求 / 响应 Schema
# ---------------------------------------------------------------------------

class ProcessRequest(BaseModel):
    template_id: str = ""       # 新用法：指定模板 id（用户自己的或系统示例）
    task_type: str = ""         # 废弃兼容：旧任务类型 key（未传 template_id 时回退）
    original_text: str = Field(..., min_length=1, max_length=4000)
    requirements: str = Field(..., min_length=1, max_length=1000)
    output_filename: str = Field(..., min_length=1, max_length=100)
    region: str = ""          # 废弃兼容：省（旧），新用 provinces
    provinces: list[str] = []  # 可选：多选省，如 ["湖北","广东"]
    cities: list[str] = []     # 可选：多选市，如 ["武汉","深圳"]
    file_types: list[str] = [] # 必选语义：多选文件类型，如 ["国家法律","地方法规"]。空列表 = 什么都不选 → 报告不引法规

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
    template_id: str = ""
    template_name: str = ""
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
    template_id: str = ""
    template_name: str = ""
    sections: list[dict] | None = None   # 逐节结果 [{no,title,instruction,content,status}]；旧文档为 null
    original_text: str
    requirements: str
    report_content: str | None = None
    created_at: str
    completed_at: str | None = None

class DocumentStatsResponse(BaseModel):
    queued: int
    processing: int
    completed: int
    failed: int

class SectionPatch(BaseModel):
    content: str = Field(..., max_length=100000, description="该节正文（前端画布手动改）")

class SectionRevise(BaseModel):
    requirements: str = Field("", max_length=4000, description="本次补充要求（纯文本）")
    materials: str = Field("", max_length=20000, description="本次补充材料/资料（纯文本）")

class SectionResult(BaseModel):
    id: str
    index: int
    no: str
    title: str
    content: str
    status: str


def _template_name_of(doc, user_id: int) -> str:
    """取模板名（尽力而为）；已删除/无权 → 兜底文案。"""
    if not doc.template_id:
        return ""
    try:
        return template_service.get_visible(user_id, doc.template_id).name
    except (KeyError, ValueError):
        return "(已删除模板)"

# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("/documents/stats", response_model=DocumentStatsResponse)
async def get_document_stats(_user: dict = Depends(perm_sys_sec_aud)):
    """获取文档处理统计信息"""
    stats = await document_service.get_stats()
    return DocumentStatsResponse(
        queued=stats["queued"],
        processing=stats["processing"],
        completed=stats["completed"],
        failed=stats["failed"],
    )


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    status: str | None = Query(None, description="按状态过滤: pending/queued/processing/completed/failed"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    _user: dict = Depends(perm_user_sys_sec_aud),
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
                detail=f"无效的状态值: {status}，允许: pending/queued/processing/completed/failed",
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
                template_id=doc.template_id,
                template_name=_template_name_of(doc, _user["id"]),
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
async def process(req: ProcessRequest, request: Request, _user: dict = Depends(perm_user_sys_sec)):
    try:
        doc = await document_service.create_document(
            template_id=req.template_id,
            task_type=req.task_type,
            original_text=req.original_text,
            requirements=req.requirements,
            output_filename=req.output_filename,
            user_id=_user["id"],
            region=req.region,
            provinces=",".join(req.provinces),
            cities=",".join(req.cities),
            file_types=",".join(req.file_types),
        )
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"无效的任务类型: {e}")
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    # 只建记录，worker 协程会自己认领处理（见 document_service.worker）
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(
        _user, "create_document", target=doc.id,
        detail=f"task_type={req.task_type}", ip=ip,
    )
    return ProcessResponse(
        id=doc.id,
        status=doc.status.value,
        output_filename=doc.report_filename,
        message="已开始处理",
    )


@router.get("/documents/{doc_id}", response_model=DocumentDetail)
async def get_document(doc_id: str, _user: dict = Depends(perm_user_sys_sec_aud)):
    try:
        doc = await document_service.get_detail(doc_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return DocumentDetail(
        id=doc.id,
        status=doc.status.value,
        output_filename=doc.report_filename,
        task_type=doc.task_type,
        template_id=doc.template_id,
        template_name=_template_name_of(doc, _user["id"]),
        sections=doc.sections,
        original_text=doc.original_text,
        requirements=doc.requirements,
        report_content=doc.report_content,
        created_at=doc.created_at,
        completed_at=doc.completed_at,
    )


@router.patch("/documents/{doc_id}/sections/{section_index}", response_model=SectionResult)
async def save_section(doc_id: str, section_index: int, req: SectionPatch,
                       request: Request, _user: dict = Depends(perm_user_sys_sec)):
    """手动改某一节内容（前端画布保存），重排整篇 .md。"""
    try:
        doc = await document_service.save_section(doc_id, section_index, req.content)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    sec = doc.sections[section_index]
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(
        _user, "edit_section", target=doc_id, detail=f"section={section_index}", ip=ip,
    )
    return SectionResult(
        id=doc.id, index=section_index, no=section_no(section_index),
        title=sec.get("title", ""), content=sec.get("content", ""), status=sec.get("status", ""),
    )


@router.post("/documents/{doc_id}/sections/{section_index}/revise", response_model=SectionResult)
async def revise_section(doc_id: str, section_index: int, req: SectionRevise,
                         request: Request, _user: dict = Depends(perm_user_sys_sec)):
    """对某一节追加"要求/材料"并让 AI 只重生成该节（其余章节作上下文）。"""
    try:
        doc = await document_service.revise_section(
            doc_id, section_index, req.requirements, req.materials
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    sec = doc.sections[section_index]
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(
        _user, "revise_section", target=doc_id,
        detail=f"section={section_index}", ip=ip,
    )
    return SectionResult(
        id=doc.id, index=section_index, no=section_no(section_index),
        title=sec.get("title", ""), content=sec.get("content", ""), status=sec.get("status", ""),
    )

@router.post("/documents/{doc_id}/retry", response_model=ProcessResponse)
async def retry(doc_id: str, request: Request, _user: dict = Depends(perm_user_sys_sec)):
    """重试处理失败的文档（只允许失败状态的文档重试）。"""
    try:
        doc = await document_service.retry_document(doc_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    ip = request.client.host if request.client else ""
    operation_log_service.record_user(_user, "retry_document", target=doc_id, ip=ip)
    return ProcessResponse(
        id=doc.id,
        status=doc.status.value,
        output_filename=doc.report_filename,
        message="已重新排队处理",
    )


@router.get("/documents/{doc_id}/download")
async def download(doc_id: str, format: str = Query("md"), _user: dict = Depends(perm_user_sys_sec_aud)):
    """下载报告：format=md 返回原 .md；format=docx 用 pandoc 转 Word。

    Word 是按需派生的：临时文件转完即返回，响应后由 BackgroundTask 清理，不落库。
    """
    try:
        doc = await document_service.get_detail(doc_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    path = report_path(doc)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="报告文件不存在")

    if format == "md":
        return FileResponse(
            path=path,
            filename=doc.report_filename,
            media_type="text/markdown; charset=utf-8",
        )

    if format == "docx":
        fd, tmp = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        try:
            await doc_exporter.md_to_docx(path, tmp)
        except RuntimeError as e:
            os.unlink(tmp)
            raise HTTPException(status_code=503, detail=f"转换失败: {e}")
        filename = os.path.splitext(doc.report_filename)[0] + ".docx"
        return FileResponse(
            path=tmp,
            filename=filename,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            background=BackgroundTask(os.unlink, tmp),
        )

    raise HTTPException(status_code=400, detail=f"仅支持 md/docx，收到: {format}")


@router.delete("/documents/{doc_id}", status_code=204)
async def delete(doc_id: str, request: Request, _user: dict = Depends(perm_user_sys_sec)):
    try:
        await document_service.delete_document(doc_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    ip = request.client.host if request.client else ""
    operation_log_service.record_user(_user, "delete_document", target=doc_id, ip=ip)


