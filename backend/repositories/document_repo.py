"""数据层 — 文档的 SQLite 读写。

元数据和原文存在 SQLite 表 documents 中：
    data/saferag.db  ← 一行一条记录

报告正文仍以文件存储（方便下载）：
    data/documents/{doc_id}/report.md
"""

import os
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from backend.config import DATA_DIR
from backend.database import get_connection

logger = logging.getLogger(__name__)

ROOT = os.path.join(DATA_DIR, "documents")


# ---------------------------------------------------------------------------
# 数据结构（不变）
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
    """文档的文件目录（只存报告 .md）。"""
    return os.path.join(ROOT, doc_id)


def save(doc: Document) -> Document:
    """新建文档，写入 SQLite。"""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO documents
               (id, status, output_filename, requirements, original_text,
                processing_note, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.id,
                doc.status.value,
                doc.output_filename,
                doc.requirements,
                doc.original_text,
                doc.processing_note,
                doc.created_at,
                doc.completed_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("文档 %s 已保存 (status=%s)", doc.id, doc.status.value)
    return doc


def get(doc_id: str) -> Document | None:
    """从 SQLite 读取元数据，从文件系统读取报告正文。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    doc = Document(
        id=row["id"],
        output_filename=row["output_filename"],
        requirements=row["requirements"],
        original_text=row["original_text"],
        status=DocStatus(row["status"]),
        processing_note=row["processing_note"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )

    # 报告正文仍在文件中
    rpt = os.path.join(_doc_dir(doc_id), doc.report_filename)
    if os.path.isfile(rpt):
        with open(rpt, "r", encoding="utf-8") as f:
            doc.report_content = f.read()

    return doc


def update(doc: Document) -> Document:
    """更新 SQLite 中的元数据，同时写报告文件。"""
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE documents
               SET status=?, output_filename=?, requirements=?, original_text=?,
                   processing_note=?, created_at=?, completed_at=?
               WHERE id=?""",
            (
                doc.status.value,
                doc.output_filename,
                doc.requirements,
                doc.original_text,
                doc.processing_note,
                doc.created_at,
                doc.completed_at,
                doc.id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # 报告正文写文件
    if doc.report_content is not None:
        d = _doc_dir(doc.id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, doc.report_filename), "w", encoding="utf-8") as f:
            f.write(doc.report_content)

    logger.info("文档 %s 已更新 (status=%s)", doc.id, doc.status.value)
    return doc


def report_path(doc: Document) -> str:
    """生成报告的绝对路径（供下载用）。"""
    return os.path.join(_doc_dir(doc.id), doc.report_filename)


def list_all(
    status: DocStatus | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[Document]:
    """列出文档（按创建时间倒序），可按状态过滤。这是 SQLite 带来的新能力。"""
    conn = get_connection()
    try:
        if status is not None:
            rows = conn.execute(
                """SELECT * FROM documents
                   WHERE status = ?
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (status.value, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM documents
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
    finally:
        conn.close()

    docs = []
    for row in rows:
        doc = Document(
            id=row["id"],
            output_filename=row["output_filename"],
            requirements=row["requirements"],
            original_text=row["original_text"],
            status=DocStatus(row["status"]),
            processing_note=row["processing_note"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )
        # 列表不读报告正文，前端点进去再请求详情
        docs.append(doc)
    return docs


def count(status: DocStatus | None = None) -> int:
    """文档总数，可按状态过滤。"""
    conn = get_connection()
    try:
        if status is not None:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM documents WHERE status = ?",
                (status.value,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM documents").fetchone()
        return row["cnt"]
    finally:
        conn.close()
