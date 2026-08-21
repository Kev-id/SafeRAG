"""数据层 — 文档树仓库（SQLite，结构真相源）。

文档树是"解析 → 入库"之间的活合同：解析段调 save() 落盘树，入库段调 load() 取树再切块。
存 SQLite 而非磁盘侧车：与 kb_files 同库、查询统一、删文件时能联动删（不漂移成孤儿）。

树与文件 1:1，filename 主键。
"""

import json
import logging
from datetime import datetime, timezone

from backend.core.database import get_connection

logger = logging.getLogger(__name__)


def save(filename: str, tree_data: dict, md5: str) -> None:
    """落盘一个文件的文档树。主键 filename 冲突则更新。"""
    payload = json.dumps(tree_data, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO kb_trees (filename, tree_json, md5, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(filename) DO UPDATE SET
                   tree_json = excluded.tree_json,
                   md5 = excluded.md5,
                   created_at = excluded.created_at""",
            (filename, payload, md5, now),
        )
        conn.commit()
    finally:
        conn.close()


def load(filename: str) -> dict | None:
    """取一个文件的文档树；不存在返回 None。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT tree_json FROM kb_trees WHERE filename = ?", (filename,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["tree_json"]) if row else None


def delete(filename: str) -> bool:
    """删一个文件的文档树；删除成功返回 True。"""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM kb_trees WHERE filename = ?", (filename,))
        deleted = cur.rowcount > 0
        conn.commit()
    finally:
        conn.close()
    return deleted
