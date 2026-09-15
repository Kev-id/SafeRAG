"""数据层 — 用户自定义模板（templates 表）。

模板 = 有序的"节"列表 `[{title, instruction}]`。编号不在库里存，
由 `section_no(index)` 按索引派生（一、二、三…），增删节自动重排。

owner_id = NULL 是系统示例模板（全员可见、只读，固定 id = 旧任务类型 key）；
否则该模板属于具体用户（users.id），仅本人 + 系统示例可见。
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.core.database import get_connection

logger = logging.getLogger(__name__)

# 中文序号到"第 26 节"止（超过回退阿拉伯数字）。要求保持短，够常见文档用。
_CN_IDX = "一二三四五六七八九十"


@dataclass
class Template:
    id: str
    name: str
    description: str
    owner_id: int | None
    sections: list[dict]          # [{title, instruction}]，顺序即编号
    created_at: str
    updated_at: str

    @property
    def is_system(self) -> bool:
        return self.owner_id is None


def section_no(index: int) -> str:
    """按索引派生章节编号：0→'一、'，1→'二、'…；超 10 节回退阿拉伯。"""
    if index < len(_CN_IDX):
        return _CN_IDX[index] + "、"
    return f"{index + 1}、"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_template(row) -> Template:
    return Template(
        id=row["id"],
        name=row["name"],
        description=row["description"] or "",
        owner_id=row["owner_id"],
        sections=json.loads(row["sections_json"] or "[]"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create(name: str, description: str, sections: list[dict],
           owner_id: int | None, template_id: str | None = None) -> Template:
    """新建模板。owner_id=None 表示系统示例；template_id 可指定（seed 用固定 id）。"""
    tid = template_id or uuid.uuid4().hex[:12]
    ts = _now()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO templates (id, name, description, owner_id, sections_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (tid, name, description, owner_id,
             json.dumps(sections, ensure_ascii=False), ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return get(tid)


def get(template_id: str) -> Template | None:
    """按 id 取模板，不存在返回 None。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_template(row) if row else None


def list_system() -> list[Template]:
    """全部系统示例模板（owner_id IS NULL）。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM templates WHERE owner_id IS NULL ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_template(r) for r in rows]


def list_mine(user_id: int) -> list[Template]:
    """该用户可见的模板：自己的 + 系统示例（系统示例排前）。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM templates
               WHERE owner_id IS NULL OR owner_id = ?
               ORDER BY (owner_id IS NULL) DESC, created_at""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_template(r) for r in rows]


def update(template_id: str, *, name: str | None = None,
           description: str | None = None, sections: list[dict] | None = None) -> Template | None:
    """按需更新模板，返回更新后的对象；不存在返回 None。"""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
        if row is None:
            return None
        if name is not None:
            conn.execute("UPDATE templates SET name=? WHERE id=?", (name, template_id))
        if description is not None:
            conn.execute("UPDATE templates SET description=? WHERE id=?", (description, template_id))
        if sections is not None:
            conn.execute(
                "UPDATE templates SET sections_json=? WHERE id=?",
                (json.dumps(sections, ensure_ascii=False), template_id),
            )
        conn.execute(
            "UPDATE templates SET updated_at=? WHERE id=?",
            (_now(), template_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get(template_id)


def delete(template_id: str) -> bool:
    """删除模板，返回是否删到了。"""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()