"""操作日志 repo 单元测试。

复用 test_legal_parser 的隔离约定：先把 DATABASE_URL 指到临时文件，
再 import backend.core.database（它在 import 时就读取 DATABASE_URL 定 _DB_PATH），
这样测试不会碰到生产 saferag.db。
"""

import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

from backend.core.database import init_db  # noqa: E402
from backend.repositories import operation_log_repo  # noqa: E402


def setup_module():
    init_db()


def _add(action="login", username="sysadmin", **kw):
    return operation_log_repo.add(
        ts=operation_log_repo.now_utc(),
        username=username,
        role="audadmin",  # 角色取审计角色，方便观察
        action=action,
        **kw,
    )


def test_add_and_read_back():
    _add(username="sysadmin", action="create_document", target="doc-1", ip="127.0.0.1")
    rows, total = operation_log_repo.list_logs(0, 50)
    assert total >= 1
    row = next(r for r in rows if r["target"] == "doc-1")
    assert row["username"] == "sysadmin"
    assert row["action"] == "create_document"
    assert row["ip"] == "127.0.0.1"
    assert row["success"] == 1
    assert row["role"] == "audadmin"


def test_filter_by_username_and_action():
    _add(username="aaa", action="upload_kb_file", target="x.txt")
    rows, total = operation_log_repo.list_logs(0, 50, username="aaa")
    assert total >= 1
    assert all(r["username"] == "aaa" for r in rows)
    rows, total = operation_log_repo.list_logs(0, 50, username="aaa", action="upload_kb_file")
    assert total >= 1
    assert all(r["action"] == "upload_kb_file" for r in rows)


def test_filter_by_success():
    _add(username="bbb", action="delete_file", target="y.txt", success=0)
    rows, total = operation_log_repo.list_logs(0, 50, success=0)
    assert total >= 1
    assert all(r["success"] == 0 for r in rows)


def test_pagination_and_order_desc():
    for _ in range(5):
        _add(username="ccc", action="login", target="z")
    rows_page1, total = operation_log_repo.list_logs(0, 2)
    rows_page2, total2 = operation_log_repo.list_logs(2, 2)
    assert total == total2
    assert len(rows_page1) == 2
    # 按时间倒序：第二页的首条不晚于第一页的末条（同秒则 id 更小）
    assert rows_page2[0]["ts"] <= rows_page1[-1]["ts"]