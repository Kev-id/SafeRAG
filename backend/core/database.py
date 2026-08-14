"""SQLite 数据库初始化与连接管理。

使用 Python 内置 sqlite3，零额外依赖。
表结构只存元数据 + 原文，报告正文仍以 .md 文件存储。
"""

import sqlite3
import os
import logging

from backend.core.config import DATABASE_URL

logger = logging.getLogger(__name__)

# sqlite:/// 前缀去掉，得到实际文件路径
_DB_PATH = DATABASE_URL.replace("sqlite:///", "")

# 确保 data 目录存在
os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """获取一个 SQLite 连接（每次调用新建）。"""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row  # 让查询结果可以用 row["column"] 访问
    conn.execute("PRAGMA journal_mode=WAL")  # 写不阻塞读
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """建表（幂等 — 表不存在才建）。启动时调用一次。"""
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id              TEXT PRIMARY KEY,
                status          TEXT NOT NULL DEFAULT 'pending',
                output_filename TEXT NOT NULL DEFAULT 'report',
                requirements    TEXT NOT NULL DEFAULT '',
                original_text   TEXT NOT NULL DEFAULT '',
                task_type       TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                completed_at    TEXT
            )
        """)
        conn.commit()
        logger.info("数据库初始化完成: %s", _DB_PATH)
    finally:
        conn.close()
