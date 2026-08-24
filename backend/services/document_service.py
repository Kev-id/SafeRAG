"""业务层 — 文档处理核心逻辑（SQLite 任务队列）。

队列 = documents 表，status 是唯一真相：queued → processing → completed/failed。
HTTP 只建记录 + 唤醒；常驻 worker 协程认领 queued 任务，单协程串行处理。
"""

import asyncio
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
    claim_next,
)

logger = logging.getLogger(__name__)

# 唤醒信号：新任务入队时 set()，worker 从等待中醒来。
# 只是闹钟，不是正确性依赖——任务真相在 SQLite 表里，事件丢了也不丢任务。
_wake = asyncio.Event()


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


def retrieve_with_citations(original_text: str, top_k: int = 5) -> tuple[str, list[str]]:
    """检索相关法规，返回 (带编号的 context 文本, 来源清单)。

    文档处理和聊天共用的检索入口：
    context 每条法规带 [编号]（来源：文件 第N条），供模型在回答/报告里标注引用；
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
        context_lines.append(f"[{i}]（{src}）{h['text']}")
        source_lines.append(f"[{i}] {src} {h['text']}")
    return "\n".join(context_lines), source_lines


async def create_document(task_type, original_text,requirements, output_filename) -> Document:
    """只建记录 + 标记 queued，不碰模型，立即返回。"""
    template = get_template(task_type)
    doc = Document(
        original_text=original_text,
        requirements=requirements,
        output_filename=output_filename,
        task_type=task_type,
        status=DocStatus.QUEUED,
    )
    save(doc)
    _wake.set()  # 唤醒 worker 来取（队列空了才真睡，多余 set 无害）
    return doc

async def _process_document(doc: Document) -> None:
    """处理一条已认领的任务：检索 → 调 Qwen → 存结果。异常置 failed。

    processing 状态由 claim_next 原子写入，这里只跑内容，不再改状态。
    """
    template = get_template(doc.task_type)
    try:
        # 检索是 CPU 密集（jieba + BM25 + embedding），丢线程池，别阻塞事件循环
        context, sources = await asyncio.to_thread(retrieve_with_citations, doc.original_text)
        messages = _build_messages(template, doc.original_text, doc.requirements, context)
        raw = await qwen_chat(messages)
    except Exception:
        logger.exception("推理失败: doc_id=%s", doc.id)
        doc.status = DocStatus.FAILED
        update(doc)
        return

    # 报告正文 + 末尾追加「参考法规来源」清单，实现引用可追溯
    report = raw.strip()
    if sources:
        # 每条来源用空行隔开：单 \n 在 Markdown 里是"软换行"（同一段），
        # pandoc 转 Word 会粘连成一段；\n\n 才各自成独立段落
        report += "\n\n---\n\n## 参考法规来源\n\n" + "\n\n".join(sources)

    doc.report_content = report
    doc.status = DocStatus.COMPLETED
    doc.completed_at = datetime.now(timezone.utc).isoformat()
    update(doc)


async def worker() -> None:
    """常驻消费协程：认领下一条 queued → 处理 → 取下一条。

    单协程天然串行，替代原来的 asyncio.Lock。没任务时睡在 _wake 上，
    被新任务唤醒；事件丢失也不影响——表里的任务下一轮必被认领。
    """
    logger.info("文档处理 worker 已启动")
    while True:
        doc = claim_next()
        if doc is None:
            await _wake.wait()
            _wake.clear()
            continue
        await _process_document(doc)

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

async def get_stats() -> dict:
    """获取文档统计信息"""
    return {
        "queued": count(DocStatus.QUEUED),
        "processing": count(DocStatus.PROCESSING),
        "completed": count(DocStatus.COMPLETED),
        "failed": count(DocStatus.FAILED),
    }

async def retry_document(doc_id: str) -> Document:
    doc = get(doc_id)
    if doc is None:
        raise FileNotFoundError(f"文档不存在: {doc_id}")
    if doc.status != DocStatus.FAILED:
        raise ValueError(f"文档状态不是 failed，不能重试: {doc.status.value}")
    doc.status = DocStatus.QUEUED
    doc.completed_at = None
    update(doc)
    _wake.set()

    return doc
