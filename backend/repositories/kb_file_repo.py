"""数据层 — 知识库文件登记册（SQLite 权威源）。

元数据存在 SQLite 表 kb_files 中（data/saferag.db）：
    - 这是知识库文件的**权威源**（Master）
    - ChromaDB 只是派生索引（检索引擎）
    - 文件本体仍在源目录磁盘

本 repo 提供登记册的增删查，不含任何"对账"逻辑——
写操作以本表为主，ChromaDB 由上层（kb_store）跟着动。
"""

import logging
from typing import Optional

from backend.core.database import get_connection

logger = logging.getLogger(__name__)


def upsert(kf: dict) -> None:
    """登记/更新一个文件。主键 filename 冲突则更新。"""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO kb_files
               (filename, md5, file_type, size, chunk_count, status, message, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(filename) DO UPDATE SET
                   md5 = excluded.md5,
                   file_type = excluded.file_type,
                   size = excluded.size,
                   chunk_count = excluded.chunk_count,
                   status = excluded.status,
                   message = excluded.message,
                   updated_at = excluded.updated_at""",
            (
                kf["filename"],
                kf.get("md5"),
                kf.get("file_type"),
                kf.get("size"),
                kf.get("chunk_count", 0),
                kf.get("status", "building"),
                kf.get("message"),
                kf.get("updated_at"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get(filename: str) -> dict | None:
    """按文件名查一个文件；不存在返回 None。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM kb_files WHERE filename = ?", (filename,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_files(file_type:Optional[str]=None, status: Optional[str]=None, keyword: Optional[str]=None) -> list[dict]:
    """列出所有指定类型的登记文件（按文件名排序）。"""
    conditions, params = [], []
    if file_type:
        conditions.append("file_type = ?")
        params.append(file_type)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if keyword:
        conditions.append("filename LIKE ?")
        params.append(f"%{keyword}%")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""#这行意思是，如果有条件，就加上 WHERE 子句，否则为空字符串。
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM kb_files{where} ORDER BY filename",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def delete(filename: str) -> bool:
    """按文件名删一行；删除成功返回 True。"""
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM kb_files WHERE filename = ?", (filename,)
        )
        deleted = cur.rowcount > 0
        conn.commit()
    finally:
        conn.close()
    return deleted
