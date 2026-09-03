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
                completed_at    TEXT,
                region          TEXT NOT NULL DEFAULT '',
                provinces       TEXT NOT NULL DEFAULT '',
                cities          TEXT NOT NULL DEFAULT '',
                file_types      TEXT NOT NULL DEFAULT ''
            )
        """)
        # region 列（事发地，用于检索地域过滤）；幂等迁移，首次不存在才加
        doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        if "provinces" not in doc_cols:
            conn.execute("ALTER TABLE documents ADD COLUMN provinces TEXT NOT NULL DEFAULT ''")
        if "cities" not in doc_cols:
            conn.execute("ALTER TABLE documents ADD COLUMN cities TEXT NOT NULL DEFAULT ''")
        if "file_types" not in doc_cols:
            conn.execute("ALTER TABLE documents ADD COLUMN file_types TEXT NOT NULL DEFAULT ''")
        if "region" not in doc_cols:
            conn.execute("ALTER TABLE documents ADD COLUMN region TEXT NOT NULL DEFAULT ''")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_files (
                filename     TEXT PRIMARY KEY,      -- 源文件名，就是知识库文档的 key
                md5          TEXT,                  -- 内容指纹（build 判变更用）
                file_type    TEXT,                  -- 文件种类（国家法律，行政法规，地方法规等）
                region       TEXT,                  -- 地域（省份/直辖市/自治区），空表示全国性法规
                size         INTEGER,               -- 字节数
                chunk_count  INTEGER,               -- 切了多少块
                updated_at   TEXT,                  -- 最近一次同步时间
                status       TEXT NOT NULL DEFAULT 'ready',   -- ready / failed
                message      TEXT                   -- 失败原因
            )
        """)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(kb_files)")}
        if "file_type" not in cols:
            conn.execute("ALTER TABLE kb_files ADD COLUMN file_type TEXT")
        if "region" not in cols:
            conn.execute("ALTER TABLE kb_files ADD COLUMN region TEXT")
        if "city" not in cols:
            conn.execute("ALTER TABLE kb_files ADD COLUMN city TEXT")
        if "sensitive" not in cols:
            conn.execute("ALTER TABLE kb_files ADD COLUMN sensitive INTEGER NOT NULL DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_trees (
                filename   TEXT PRIMARY KEY,
                tree_json  TEXT NOT NULL,
                md5        TEXT,
                created_at TEXT NOT NULL
            )
        """)
        # 认证用户表（三权分立账号 sysadmin/secadmin/audadmin）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL,
                full_name     TEXT NOT NULL DEFAULT '',
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL
            )
        """)
        # 操作日志表（审计留痕：谁在何时做了什么、结果如何；仅审计管理员可查）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operation_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,             -- 操作时间（UTC）
                username TEXT NOT NULL,             -- 操作者
                role     TEXT NOT NULL,             -- 操作时角色快照（防角色变更）
                action   TEXT NOT NULL,             -- login / create_document / delete_file ...
                target   TEXT NOT NULL DEFAULT '',  -- 目标对象：doc_id / filename / username
                detail   TEXT NOT NULL DEFAULT '',  -- 补充说明：任务类型 / 失败原因等
                ip       TEXT NOT NULL DEFAULT '',  -- 客户端 IP
                success  INTEGER NOT NULL DEFAULT 1 -- 1 成功 / 0 失败
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operation_log_ts ON operation_log(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_operation_log_username ON operation_log(username)")
        conn.commit()
        logger.info("数据库初始化完成: %s", _DB_PATH)
    finally:
        conn.close()

def check_database_health(db_path=_DB_PATH) -> bool:
    try:
        # 连接到数据库
        conn = sqlite3.connect(db_path)
        # 执行快速健康检查
        result = conn.execute("PRAGMA quick_check;").fetchone()[0]
        conn.close()
        
        if result == "ok":
            logging.info("Database health check passed.")
            return True
        else:
            logging.error(f"Integrity issue detected: {result}")
            return False
    except Exception as e:
        logging.critical(f"Cannot access database: {str(e)}")
        return False