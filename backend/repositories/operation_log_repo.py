"""操作日志数据访问 — 记录谁在何时做了什么、结果如何。

审计留痕的存储层。写入是否成功不会影响业务主流程
（由 service 层保证：任何异常仅记 app logger，绝不向上抛）。
"""

from datetime import datetime, timezone

from backend.core.database import get_connection


def now_utc() -> str:
    """当前 UTC 时间，格式与 user_repo 一致（"%Y-%m-%d %H:%M:%S"）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def add(
    ts: str,
    username: str,
    role: str,
    action: str,
    target: str = "",
    detail: str = "",
    ip: str = "",
    success: int = 1,
) -> None:
    """写入一条操作日志。"""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO operation_log
               (ts, username, role, action, target, detail, ip, success)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, username, role, action, target, detail, ip, success),
        )
        conn.commit()
    finally:
        conn.close()


def _build_where(
    username: str | None,
    action: str | None,
    success: int | None,
) -> tuple[str, list]:
    """按过滤条件拼 WHERE 子句（值全部走参数绑定，防注入）。"""
    conds: list[str] = []
    params: list = []
    if username:
        conds.append("username = ?")
        params.append(username)
    if action:
        conds.append("action = ?")
        params.append(action)
    if success is not None:
        conds.append("success = ?")
        params.append(success)
    where = " AND ".join(conds) if conds else "1=1"
    return where, params


def list_logs(
    offset: int,
    limit: int,
    username: str | None = None,
    action: str | None = None,
    success: int | None = None,
) -> tuple[list[dict], int]:
    """分页查询操作日志，按时间倒序（同秒按 id 倒序）；返回 (rows, total)。"""
    where, params = _build_where(username, action, success)
    conn = get_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM operation_log WHERE {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT id, ts, username, role, action, target, detail, ip, success
                FROM operation_log WHERE {where}
                ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], int(total)
    finally:
        conn.close()