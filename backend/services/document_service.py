"""业务层 — 文档处理核心逻辑。

流程: 保存 → 拼 Prompt → 调 Qwen → 解析 → 存储结果
"""

import logging
from datetime import datetime, timezone

from backend.qwen_client import chat as qwen_chat
from backend.repositories.document_repo import (
    Document,
    DocStatus,
    save,
    get,
    update,
    list_all,
    count,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是一个安全生产专家，擅长根据事故信息生成专业的安全分析报告。"
    "请严格按照用户提供的原始文档和处理要求来整理报告。"
)


def _build_messages(original_text: str, requirements: str) -> list[dict]:
    user_content = f"""请根据以下信息生成安全报告。

【事故原始文档】
{original_text}

【处理要求】
{requirements}

请按以下结构输出报告（使用 Markdown 格式）：

## 事故概述
## 原因分析
## 法规依据
## 处理建议

最后附上【修改说明】，简要说明你对原始文档做了哪些整理和补充。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _parse_output(raw: str) -> tuple[str, str | None]:
    """从 Qwen 输出中分离报告正文和修改说明。"""
    import re
    for marker in [r"\n\s*【修改说明】", r"\n\s*##\s*修改说明"]:
        m = re.search(marker, raw)
        if m:
            report = raw[: m.start()].strip()
            note = raw[m.start() :].strip()
            return report, note
    return raw.strip(), None


async def process(original_text: str, requirements: str, output_filename: str) -> Document:
    """处理文档：调 Qwen 生成报告并存储。"""
    # 1. 新建
    doc = Document(
        original_text=original_text,
        requirements=requirements,
        output_filename=output_filename,
    )
    save(doc)

    # 2. 处理中
    doc.status = DocStatus.PROCESSING
    update(doc)

    # 3. 调 Qwen
    try:
        messages = _build_messages(original_text, requirements)
        raw = await qwen_chat(messages)
    except Exception:
        doc.status = DocStatus.FAILED
        update(doc)
        raise

    # 4. 解析 & 完成
    report, note = _parse_output(raw)
    doc.report_content = report
    doc.processing_note = note
    doc.status = DocStatus.COMPLETED
    doc.completed_at = datetime.now(timezone.utc).isoformat()
    update(doc)

    return doc


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
