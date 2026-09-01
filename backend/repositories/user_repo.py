"""认证用户仓储：users 表 CRUD + 三权分立种子账号。

启动时调用 seed_users() 幂等创建 sysadmin/secadmin/audadmin 三个账号。
"""

import logging
from datetime import datetime, timezone

from backend.core.config import SEED_PASSWORDS
from backend.core.database import get_connection
from backend.core.security import hash_password

logger = logging.getLogger(__name__)

# 角色表：key=登录角色，value=角色显示名
ROLES = {
    "user": "普通用户",
    "sysadmin": "系统管理员",
    "secadmin": "安全管理员",
    "audadmin": "审计管理员",
}

# seed 仅创建三员（普通用户由 sysadmin 通过建号接口创建）
SEED_ROLES = ("sysadmin", "secadmin", "audadmin")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def seed_users() -> None:
    """幂等创建三员种子账号（表不存在才插入）。启动时调用一次。"""
    conn = get_connection()
    try:
        for username in SEED_ROLES:
            full_name = ROLES[username]
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


def get_user_by_id(user_id: int) -> dict | None:
    """按 id 查用户（不含密码哈希）。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, username, role, full_name, is_active, created_at "
            "FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users() -> list[dict]:
    """列出全部用户（不含密码哈希）。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, role, full_name, is_active, created_at "
            "FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_user(username: str, full_name: str, role: str, password: str) -> dict:
    """创建用户，返回不含密码哈希的 dict；用户名重复返回 None。"""
    conn = get_connection()
    try:
        exists = conn.execute(
            "SELECT id FROM users WHERE username=?", (username,)
        ).fetchone()
        if exists is not None:
            return None
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, full_name, is_active, created_at) "
            "VALUES (?,?,?,?,1,?)",
            (username, hash_password(password), role, full_name, _now()),
        )
        conn.commit()
        return get_user_by_id(cur.lastrowid)
    finally:
        conn.close()


def update_user(user_id: int, *, full_name: str | None = None,
                role: str | None = None, is_active: bool | None = None) -> dict | None:
    """按需更新字段；返回更新后的 dict 或 None（不存在时）。"""
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            return None
        if full_name is not None:
            conn.execute("UPDATE users SET full_name=? WHERE id=?", (full_name, user_id))
        if role is not None:
            conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        if is_active is not None:
            conn.execute("UPDATE users SET is_active=? WHERE id=?", (1 if is_active else 0, user_id))
        conn.commit()
        return get_user_by_id(user_id)
    finally:
        conn.close()


def reset_password(user_id: int, new_password: str) -> bool:
    """重置密码，成功返回 True。"""
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(new_password), user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()