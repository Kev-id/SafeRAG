"""数据层 — 文档的文件系统读写。

每个文档存为一个子目录：
    data/documents/{doc_id}/
        original.txt   — 用户输入
        report.md      — AI 生成的报告
        meta.json      — 其余字段
"""

import json
import os
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "documents")


# ---------------------------------------------------------------------------
# 简单的数据结构（暂时放这里，后续可抽到 models.py）
# ---------------------------------------------------------------------------

class DocStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Document:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    original_text: str = ""
    requirements: str = ""
    output_filename: str = "report"
    status: DocStatus = DocStatus.PENDING
    report_content: str | None = None
    processing_note: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None

    @property
    def report_filename(self) -> str:
        name = self.output_filename
        if not name.endswith(".md"):
            name += ".md"
        return name


# ---------------------------------------------------------------------------
# 增删改查
# ---------------------------------------------------------------------------

def _doc_dir(doc_id: str) -> str:
    return os.path.join(ROOT, doc_id)


def save(doc: Document) -> Document:
    """新建文档并写入磁盘。"""
    d = _doc_dir(doc.id)
    os.makedirs(d, exist_ok=True)
    _write(doc)
    logger.info("文档 %s 已保存 (status=%s)", doc.id, doc.status.value)
    return doc


def get(doc_id: str) -> Document | None:
    """读取单个文档，不存在返回 None。"""
    d = _doc_dir(doc_id)
    meta_path = os.path.join(d, "meta.json")
    if not os.path.isfile(meta_path):
        return None
    return _read(doc_id)


def update(doc: Document) -> Document:
    """更新已有文档。"""
    _write(doc)
    logger.info("文档 %s 已更新 (status=%s)", doc.id, doc.status.value)
    return doc


def report_path(doc: Document) -> str:
    """生成报告的绝对路径（供下载用）。"""
    return os.path.join(_doc_dir(doc.id), doc.report_filename)


# ---------------------------------------------------------------------------
# 内部读写
# ---------------------------------------------------------------------------

def _read(doc_id: str) -> Document | None:
    d = _doc_dir(doc_id)
    meta_path = os.path.join(d, "meta.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    doc = Document(
        id=data["id"],
        output_filename=data["output_filename"],
        requirements=data.get("requirements", ""),
        status=DocStatus(data.get("status", "pending")),
        processing_note=data.get("processing_note"),
        created_at=data.get("created_at", ""),
        completed_at=data.get("completed_at"),
    )

    # 读原始文本
    orig = os.path.join(d, "original.txt")
    if os.path.isfile(orig):
        with open(orig, "r", encoding="utf-8") as f:
            doc.original_text = f.read()

    # 读报告
    rpt = os.path.join(d, doc.report_filename)
    if os.path.isfile(rpt):
        with open(rpt, "r", encoding="utf-8") as f:
            doc.report_content = f.read()

    return doc


def _write(doc: Document) -> None:
    d = _doc_dir(doc.id)
    os.makedirs(d, exist_ok=True)

    meta = {
        "id": doc.id,
        "output_filename": doc.output_filename,
        "requirements": doc.requirements,
        "status": doc.status.value,
        "processing_note": doc.processing_note,
        "created_at": doc.created_at,
        "completed_at": doc.completed_at,
    }
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(d, "original.txt"), "w", encoding="utf-8") as f:
        f.write(doc.original_text)

    if doc.report_content is not None:
        with open(os.path.join(d, doc.report_filename), "w", encoding="utf-8") as f:
            f.write(doc.report_content)
