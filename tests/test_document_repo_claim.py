"""document_repo.claim_next 回归测试 — 认领后内存对象与 DB 都应是 processing。

防历史 bug：claim_next 返回的是认领前 SELECT 的旧行（内存 status 残留 queued），
若不对齐，后续任何 update(doc)（逐节进度落库）会把 DB 的 processing 覆盖回 queued，
前端一路看到"排队中"而非"处理中"。
"""

import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

import pytest  # noqa: E402

from backend.core.database import get_connection, init_db  # noqa: E402
from backend.repositories.document_repo import (  # noqa: E402
    DocStatus,
    Document,
    claim_next,
    save,
)


@pytest.fixture(autouse=True)
def _db():
    init_db()
    yield


def _queued() -> Document:
    doc = Document(original_text="x", status=DocStatus.QUEUED)
    save(doc)
    return doc


def test_claim_next_memory_status_is_processing():
    d = _queued()
    claimed = claim_next()
    assert claimed is not None and claimed.id == d.id
    assert claimed.status == DocStatus.PROCESSING     # 内存对齐，不残留 queued


def test_claim_next_db_status_is_processing():
    d = _queued()
    claim_next()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM documents WHERE id=?", (d.id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "processing"


def test_claim_next_empty_queue_returns_none():
    assert claim_next() is None