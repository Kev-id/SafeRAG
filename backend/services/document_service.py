"""业务层 — 文档处理核心逻辑（SQLite 任务队列 + 逐节顺序生成）。

队列 = documents 表，status 是唯一真相：queued → processing → completed/failed。
HTTP 只建记录 + 唤醒；常驻 worker 协程认领 queued 任务，单协程串行处理。

模板化：提交时把模板节列表做快照存进 doc.template_snapshot，worker 按快照逐节顺序生成
（后节可引用前节，见 report_builder.build_section_messages），单节失败不拖垮整篇。
旧文档（task_type 且无快照）退化为按旧模板单节点处理，保持兼容。
"""

import asyncio
import logging
from datetime import datetime, timezone

from backend.core.config import SECTION_CTX_BUDGET
from backend.core.qwen_client import chat as qwen_chat
from backend.core.retriever import get_retriever
from backend.repositories.document_repo import (
    DocStatus,
    Document,
    claim_next,
    count,
    delete,
    get,
    list_all,
    save,
    update,
)
from backend.repositories.template_repo import section_no
from backend.services.report_builder import (
    build_revise_messages,
    build_section_messages,
    render_report,
)
from backend.services.template_service import get_template, resolve_template

logger = logging.getLogger(__name__)

# 唤醒信号：新任务入队时 set()，worker 从等待中醒来。
# 只是闹钟，不是正确性依赖——任务真相在 SQLite 表里，事件丢了也不丢任务。
_wake = asyncio.Event()


def retrieve_with_citations(original_text: str, top_k: int = 5,
                            provinces: list[str] | None = None,
                            cities: list[str] | None = None,
                            file_types: list[str] | None = None) -> tuple[str, list[str]]:
    """检索相关法规，返回 (带编号的 context 文本, 来源清单)。

    文档处理和聊天共用的检索入口：
    context 每条法规带 [编号]（来源：文件 第N条），供模型在回答/报告里标注引用；
    sources 是 [编号]→来源 的清单，拼到报告末尾做「参考法规来源」附录，实现可追溯。
    provinces/cities/file_types 可选：省市区县多选 + 文件类型，透传给检索器。
    检索失败返回 ("", [])，降级为"不注入法规"的纯 LLM 生成。
    """
    try:
        hits = get_retriever().retrieve(
            original_text, top_k=top_k,
            provinces=provinces, cities=cities, file_types=file_types,
        )
    except Exception:
        logger.exception("检索知识库失败，降级为不注入法规")
        return "", []

    context_lines, source_lines = [], []
    for i, h in enumerate(hits, 1):
        src = h["meta"].get("source", "?")
        context_lines.append(f"[{i}]（{src}）{h['text']}")
        source_lines.append(f"[{i}] {src} {h['text']}")
    return "\n".join(context_lines), source_lines


def _filters_of(doc: Document) -> tuple[list[str], list[str], list[str]]:
    """把 doc 上逗号分隔的省/市/类型切成 list 用于检索过滤。

    空串 → []（而不是 None）：[] 与 None 语义不同——file_types=[] 表示
    "显式一个类型都不选 → 什么都不检索"，None 才是"未做类型筛选 → 全量"。
    """
    provinces = [p.strip() for p in (doc.provinces or "").split(",") if p.strip()]
    cities = [c.strip() for c in (doc.cities or "").split(",") if c.strip()]
    file_types = [t.strip() for t in (doc.file_types or "").split(",") if t.strip()]
    return provinces, cities, file_types


def _report_title(doc: Document) -> str:
    """报告大标题：取输出文件名（去扩展名），缺省用模板名/默认名。"""
    base = (doc.output_filename or doc.task_type or "报告").strip()
    for ext in (".md", ".txt"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
    return base or "报告"


async def create_document(template_id: str = "", task_type: str = "",
                          original_text: str = "", requirements: str = "",
                          output_filename: str = "", user_id: int = 0,
                          region: str = "", provinces: str = "", cities: str = "",
                          file_types: str = "") -> Document:
    """解析模板 → 节快照 → 只建记录 + 标记 queued，不碰模型，立即返回。

    模板不存在 → KeyError（API 层转 422）；无权使用（别人的私有模板）→ ValueError（转 403）。
    """
    tpl = resolve_template(template_id, task_type, user_id)
    doc = Document(
        original_text=original_text,
        requirements=requirements,
        output_filename=output_filename,
        task_type=task_type or (tpl.id if tpl.is_system else ""),
        status=DocStatus.QUEUED,
        region=region,
        provinces=provinces,
        cities=cities,
        file_types=file_types,
        template_id=tpl.id,
        template_snapshot=[
            {"title": s.get("title", ""), "instruction": s.get("instruction", "")}
            for s in tpl.sections
        ],
    )
    save(doc)
    _wake.set()  # 唤醒 worker 来取（队列空了才真睡，多余 set 无害）
    return doc


async def _process_document(doc: Document) -> None:
    """处理一条已认领的任务：检索 → 逐节调 Qwen → 存结果。异常置 failed。

    processing 状态由 claim_next 原子写入，这里只跑内容，不再改状态。
    """
    # 旧文档（无快照但 task_type）：按旧系统模板兜底成"逐节"，保持兼容
    snapshot = doc.template_snapshot
    if not snapshot and doc.task_type:
        try:
            tpl = get_template(doc.task_type)
        except KeyError:
            logger.error("旧文档模板缺失: doc_id=%s task_type=%s", doc.id, doc.task_type)
            doc.status = DocStatus.FAILED
            update(doc)
            return
        snapshot = [{"title": s.get("title", ""), "instruction": s.get("instruction", "")}
                    for s in tpl.sections]
        doc.template_id = tpl.id
        doc.template_snapshot = snapshot

    # 阶段一：检索（CPU 密集，丢线程池）
    try:
        provinces, cities, file_types = _filters_of(doc)
        context, sources = await asyncio.to_thread(
            retrieve_with_citations, doc.original_text, 5, provinces, cities, file_types
        )
    except Exception:
        logger.exception("检索失败: doc_id=%s", doc.id)
        context, sources = "", []

    # 初始化逐节进度：每节 an element，status queued；即时落库供前端轮询
    sections = [
        {"no": section_no(i), "title": s.get("title", ""),
         "instruction": s.get("instruction", ""),
         "content": None, "status": "queued"}
        for i, s in enumerate(snapshot)
    ]
    doc.sections = sections
    doc.sources = sources
    update(doc)

    if not snapshot:
        doc.status = DocStatus.FAILED
        update(doc)
        logger.warning("空模板不生成: doc_id=%s", doc.id)
        return

    # 阶段二：逐节顺序生成（后节引用前节）；单节失败继续，最后按有无成功判定
    for i in range(len(sections)):
        sections[i]["status"] = "generating"
        update(doc)
        try:
            messages = build_section_messages(
                doc.original_text, doc.requirements, context, sections, i, SECTION_CTX_BUDGET
            )
            content = await qwen_chat(messages)
        except Exception:
            logger.exception("章节生成失败: doc_id=%s section=%d", doc.id, i)
            sections[i]["status"] = "failed"
            update(doc)
            continue
        sections[i]["content"] = content.strip()
        sections[i]["status"] = "completed"
        sections[i]["completed_at"] = datetime.now(timezone.utc).isoformat()
        update(doc)

    # 阶段三：从节拼回整篇 .md（含参考法规来源），判终态
    doc.report_content = render_report(_report_title(doc), sections, doc.sources)
    any_ok = any(s.get("status") == "completed" for s in sections)
    doc.status = DocStatus.COMPLETED if any_ok else DocStatus.FAILED
    if any_ok:
        doc.completed_at = datetime.now(timezone.utc).isoformat()
    update(doc)


def _persist_report(doc: Document) -> None:
    """从当前 sections 重渲染 .md 并落库（单节编辑/精修后调用）。"""
    doc.report_content = render_report(_report_title(doc), doc.sections or [], doc.sources or [])
    update(doc)


async def save_section(doc_id: str, index: int, content: str) -> Document:
    """手动保存某一节内容（前端画布），重排报告 .md。"""
    doc = get(doc_id)
    if doc is None:
        raise FileNotFoundError(f"文档不存在: {doc_id}")
    if doc.sections is None or not 0 <= index < len(doc.sections):
        raise ValueError(f"章节越界: {index}")
    doc.sections[index]["content"] = content
    doc.sections[index]["status"] = "completed"
    doc.sections[index]["edited_at"] = datetime.now(timezone.utc).isoformat()
    _persist_report(doc)
    return doc


async def revise_section(doc_id: str, index: int,
                         requirements: str = "", materials: str = "") -> Document:
    """对某一节追加"要求/材料"并让 AI 只重生成该节（其余章节作上下文）。

    同步调用：调用前先把该节 status 置 generating 并落库（前端轮询详情
    GET /documents/{id} 可见"精修中"——qwen_chat 是 await 不占事件循环，
    轮询是独立并发请求）；生成完成置回 completed + revised_at 并重渲染整篇。
    失败恢复原状态（保留旧内容），再上抛。
    """
    doc = get(doc_id)
    if doc is None:
        raise FileNotFoundError(f"文档不存在: {doc_id}")
    if doc.sections is None or not 0 <= index < len(doc.sections):
        raise ValueError(f"章节越界: {index}")

    prev_status = doc.sections[index].get("status", "completed")
    doc.sections[index]["status"] = "generating"
    update(doc)   # 立即落库：前端详情能轮询到"精修中"

    try:
        # 重新检索（按文档原筛选条件），提供本章可用法规；附录来源不动
        provinces, cities, file_types = _filters_of(doc)
        context, _ = await asyncio.to_thread(
            retrieve_with_citations, doc.original_text, 5, provinces, cities, file_types
        )
        messages = build_revise_messages(
            doc.original_text, doc.requirements, context,
            doc.sections, index, requirements, materials, SECTION_CTX_BUDGET,
        )
        content = await qwen_chat(messages)
    except Exception:
        # 失败：恢复原状态（旧内容仍在），别让前端看到"该节失败但内容凭空消失"
        logger.exception("单节精修失败: doc_id=%s section=%d", doc.id, index)
        doc.sections[index]["status"] = prev_status
        update(doc)
        raise

    doc.sections[index]["content"] = content.strip()
    doc.sections[index]["status"] = "completed"
    doc.sections[index]["revised_at"] = datetime.now(timezone.utc).isoformat()
    _persist_report(doc)
    return doc


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