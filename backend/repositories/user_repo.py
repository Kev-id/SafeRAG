"""认证用户仓储：users 表 CRUD + 三权分立种子账号。

启动时调用 seed_users() 幂等创建 sysadmin/secadmin/audadmin 三个账号。
"""

import logging
from datetime import datetime, timezone

from backend.core.config import SEED_PASSWORDS
from backend.core.database import get_connection
from backend.core.security import hash_password

logger = logging.getLogger(__name__)

# 角色表：key=登录用户名，value=角色显示名
ROLES = {
    "sysadmin": "系统管理员",
    "secadmin": "安全管理员",
    "audadmin": "审计管理员",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def seed_users() -> None:
    """幂等创建种子账号（表不存在才插入）。启动时调用一次。"""
    conn = get_connection()
    try:
        for username, full_name in ROLES.items():
            row = conn.execute(
                "SELECT id FROM users WHERE username=?", (username,)
            ).fetchone()
            if row is None:
                pwd = SEED_PASSWORDS.get(username, "change-me")
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
                    "VALUES (?,?,?,?,1,?)",
                    (username, hash_password(pwd), username, full_name, _now()),
                )
                logger.info("已创建种子用户: %s (角色=%s)", username, full_name)
        conn.commit()
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    """按用户名查用户，返回 dict(row) 或 None。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, role, full_name, is_active, created_at "
            "FROM users WHERE username=?",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()