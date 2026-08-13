"""业务层 — 文档处理核心逻辑。

流程: 保存 → 拼 Prompt → 调 Qwen → 存储结果
"""

import logging
from datetime import datetime, timezone

from backend.qwen_client import chat as qwen_chat
from backend.services.template_service import get_template, PromptTemplate
from backend.repositories.document_repo import (
    Document,
    DocStatus,
    save,
    get,
    update,
    list_all,
    count,
    delete,
)

logger = logging.getLogger(__name__)


def _build_messages(template: PromptTemplate, original_text: str, requirements: str) -> list[dict]:
    """按模板拼出 system + user 两条消息。"""
    user_content = template.user_template.format(
        original_text=original_text,
        requirements=requirements,
    )
    return [
        {"role": "system", "content": template.system_prompt},
        {"role": "user", "content": user_content},
    ]


async def create_document(task_type, original_text,requirements, output_filename) -> Document:
    """只建记录 + 标记 processing，不碰模型，立即返回。"""
    template = get_template(task_type)
    doc = Document(
        original_text=original_text,
        requirements=requirements,
        output_filename=output_filename,
        task_type=task_type,
    )
    save(doc)
    doc.status = DocStatus.PROCESSING
    update(doc)
    return doc

async def run_inference(doc_id:str) -> None:
    doc = get(doc_id)
    if doc is None:
        return
    template = get_template(doc.task_type)
    try:
        messages = _build_messages(template,doc.original_text,doc.requirements)
        raw = await qwen_chat(messages)
    except Exception:
        doc.status = DocStatus.FAILED
        update(doc)
        return
    doc.report_content = raw.strip()
    doc.status = DocStatus.COMPLETED
    doc.completed_at = datetime.now(timezone.utc).isoformat()
    update(doc)

async def get_detail(doc_id: str) -> Document:
    """获取文档详情。"""
    doc = get(doc_id)
    if doc is None:
        raise FileNotFoundError(f"文档不存在: {doc_id}")
    return doc


async def list_documents(
    status: DocStatus | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """分页列出文档，可按状态过滤。

    返回: {items: [...], total: int, page: int, page_size: int}
    """
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 20
    if page_size > 100:
        page_size = 100  # 防止一次拉太多

    offset = (page - 1) * page_size
    items = list_all(status=status, limit=page_size, offset=offset)
    total = count(status=status)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

async def delete_document(doc_id: str) -> bool:
    doc = get(doc_id)
    if doc is None:
        raise FileNotFoundError(f"文档不存在: {doc_id}")
    return delete(doc_id)
