"""文档 API 契约测试：process 校验、详情带 sections、单节 PATCH / revise。

qwen_chat / retrieve 用桩；worker 不在测试里跑，process 只验"入队"与校验。
需要 SafeRAG / CI 环境（documents 路由 → document_service → retriever → jieba）。
"""

import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
_TMP_DATA = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"
os.environ["DATA_DIR"] = _TMP_DATA

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.api import documents as docs_api  # noqa: E402
from backend.api import templates as templates_api  # noqa: E402
from backend.core.database import get_connection, init_db  # noqa: E402
from backend.repositories.document_repo import DocStatus, Document, save  # noqa: E402
from backend.services import (  # noqa: E402
    auth_service,
    template_service,
)
from backend.services import (  # noqa: E402
    document_service as ds,
)


@pytest.fixture(autouse=True)
def _fresh():
    init_db()
    conn = get_connection()
    conn.execute("DELETE FROM templates")
    conn.commit()
    conn.close()
    template_service.seed_system_templates()
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(templates_api.router)
    app.include_router(docs_api.router)
    app.dependency_overrides[auth_service.get_current_user] = (
        lambda: {"id": 1, "username": "u1", "role": "user"}
    )
    return TestClient(app)


def _payload(template_id="accident_analysis"):
    return {
        "template_id": template_id,
        "original_text": "事故经过……",
        "requirements": "生成规范公文",
        "output_filename": "报告",
    }


def test_process_queued_and_detail_shape(client):
    r = client.post("/api/v1/documents/process", json=_payload())
    assert r.status_code == 201
    assert r.json()["status"] == "queued"
    did = r.json()["id"]

    d = client.get(f"/api/v1/documents/{did}").json()
    assert d["status"] == "queued"
    assert d["template_id"] == "accident_analysis"
    assert d["template_name"] == "事故分析报告"
    assert d["sections"] is None            # worker 未跑，逐节尚未生成


def test_process_bad_template_return_422(client):
    assert client.post("/api/v1/documents/process",
                       json=_payload("不存在的id")).status_code == 422
    # 即无 template_id 也无 task_type
    assert client.post("/api/v1/documents/process",
                       json={**_payload(), "template_id": ""}).status_code == 422


def test_process_others_private_template_return_403(client):
    other = template_service.create("他人模板", "", [{"title": "x", "instruction": "y"}], owner_id=2)
    assert client.post("/api/v1/documents/process",
                       json=_payload(other.id)).status_code == 403


def _completed_doc_with_sections() -> str:
    doc = Document(
        original_text="事故", requirements="要求", output_filename="报告",
        status=DocStatus.COMPLETED,
        template_id="accident_analysis",
        template_snapshot=[{"title": "甲", "instruction": "i"}],
        sections=[{"no": "一、", "title": "甲", "instruction": "i",
                   "content": "旧内容", "status": "completed"}],
        sources=[],
    )
    save(doc)
    return doc.id


def test_patch_section_manual_edit(client):
    did = _completed_doc_with_sections()
    r = client.patch(f"/api/v1/documents/{did}/sections/0", json={"content": "手改内容"})
    assert r.status_code == 200
    assert r.json()["content"] == "手改内容"

    d = ds.get(did)
    assert d.sections[0]["content"] == "手改内容"
    assert "手改内容" in d.report_content


def test_revise_section_regenerate_via_chat(monkeypatch, client):
    did = _completed_doc_with_sections()
    seen = []

    async def fake_chat(messages):
        seen.append(messages[1]["content"])
        return "AI 新内容"

    monkeypatch.setattr(ds, "qwen_chat", fake_chat)
    monkeypatch.setattr(ds, "retrieve_with_citations", lambda *a, **k: ("法规", ["[1] a.txt"]))

    r = client.post(f"/api/v1/documents/{did}/sections/0/revise",
                    json={"requirements": "补要求", "materials": "补材料"})
    assert r.status_code == 200
    assert r.json()["content"] == "AI 新内容"
    assert r.json()["status"] == "completed"
    assert "补材料" in seen[0]              # 补充材料确实进了 prompt

    d = ds.get(did)
    assert d.sections[0]["content"] == "AI 新内容"
    assert "AI 新内容" in d.report_content