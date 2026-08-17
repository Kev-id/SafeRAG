"""业务层 — 文档处理核心逻辑。

流程: 保存 → 拼 Prompt → 调 Qwen → 存储结果
"""

import logging
from datetime import datetime, timezone

from backend.core.qwen_client import chat as qwen_chat
from backend.core.retriever import get_retriever
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


def _build_messages(template: PromptTemplate, original_text: str, requirements: str, context: str = "") -> list[dict]:
    """按模板拼出 system + user 两条消息。context 是检索到的法规条文。"""
    user_content = template.user_template.format(
        original_text=original_text,
        requirements=requirements,
        context=context,
    )
    return [
        {"role": "system", "content": template.system_prompt},
        {"role": "user", "content": user_content},
    ]


def _retrieve_context(original_text: str, top_k: int = 5) -> tuple[str, list[str]]:
    """检索相关法规，返回 (带编号的 context 文本, 来源清单)。

    context 每条法规带 [编号]（来源：文件 第N条），供模型在报告里标注引用；
    sources 是 [编号]→来源 的清单，拼到报告末尾做「参考法规来源」附录，实现可追溯。
    检索失败返回 ("", [])，降级为"不注入法规"的纯 LLM 生成。
    """
    try:
        hits = get_retriever().retrieve(original_text, top_k=top_k)
    except Exception:
        logger.exception("检索知识库失败，降级为不注入法规")
        return "", []

    context_lines, source_lines = [], []
    for i, h in enumerate(hits, 1):
        src = h["meta"].get("source", "?")
        chunk = h["meta"].get("chunk", "?")
        context_lines.append(f"[{i}]（{src} 第{chunk}条）{h['text']}")
        source_lines.append(f"[{i}] {src} 第{chunk}条：{h['text']}")
    return "\n".join(context_lines), source_lines


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
        context, sources = _retrieve_context(doc.original_text)  # RAG：先检索相关法规
        messages = _build_messages(template,doc.original_text,doc.requirements,context)
        raw = await qwen_chat(messages)
    except Exception:
        logger.exception("推理失败: doc_id=%s", doc_id)
        doc.status = DocStatus.FAILED
        update(doc)
        return

    # 报告正文 + 末尾追加「参考法规来源」清单，实现引用可追溯
    report = raw.strip()
    if sources:
        report += "\n\n---\n\n## 参考法规来源\n" + "\n".join(sources)

    doc.report_content = report
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
