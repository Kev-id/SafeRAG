from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from backend.services.knowledge_service import upload_kb_file

router = APIRouter(prefix="/api/v1")

class KbUploadResponse(BaseModel):
    message: str


@router.post("/files",response_model=KbUploadResponse,status_code=201)
async def upload_file(file: UploadFile=File(...)):#file: UploadFile=File(...) 表示 file 参数是一个 UploadFile 类型的变量，且默认值为 File(...)，即 FastAPI 会自动处理文件上传的请求
    """上传 txt 文件并增量重建知识库索引。"""
    content = await file.read()
    try:
        return await upload_kb_file(file.filename or "", content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))