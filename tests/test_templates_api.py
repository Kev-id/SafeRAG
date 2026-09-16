"""模板 API 契约测试（TestClient + 鉴权桩）。

不依赖 jieba（templates 路由只走 template_service）。临时库隔离。
"""

import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.api import templates as templates_api  # noqa: E402
from backend.core.database import get_connection, init_db  # noqa: E402
from backend.services import auth_service, template_service  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    """每次测试清空模板表再 seed，隔离用例。"""
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
    # 用桩替换鉴权：当前用户 = 普通用户 id=1
    app.dependency_overrides[auth_service.get_current_user] = (
        lambda: {"id": 1, "username": "u1", "role": "user"}
    )
    return TestClient(app)


def _section(title="甲", instruction="写甲"):
    return {"title": title, "instruction": instruction}


def test_list_returns_system_templates(client):
    r = client.get("/api/v1/templates")
    assert r.status_code == 200
    ts = r.json()["templates"]
    assert len(ts) == 3
    assert all(t["is_system"] for t in ts)
    assert all(t["sections"] and t["sections"][0]["no"] == "一、" for t in ts)


def test_create_own_template_then_patch_and_delete(client):
    r = client.post("/api/v1/templates", json={
        "name": "我的模板", "description": "d",
        "sections": [_section("甲"), _section("乙")],
    })
    assert r.status_code == 201
    item = r.json()
    assert item["is_system"] is False
    assert [s["no"] for s in item["sections"]] == ["一、", "二、"]

    tid = item["id"]
    r2 = client.patch(f"/api/v1/templates/{tid}", json={"name": "改名"})
    assert r2.status_code == 200 and r2.json()["name"] == "改名"
    assert client.delete(f"/api/v1/templates/{tid}").status_code == 204
    assert client.get(f"/api/v1/templates/{tid}").status_code == 404


def test_system_template_readonly(client):
    assert client.patch("/api/v1/templates/accident_analysis",
                        json={"name": "x"}).status_code == 403
    assert client.delete("/api/v1/templates/accident_analysis").status_code == 403


def test_others_private_template_hidden_and_readonly(client):
    other = template_service.create("他人模板", "", [_section()], owner_id=2)
    assert client.get(f"/api/v1/templates/{other.id}").status_code == 403
    assert client.patch(f"/api/v1/templates/{other.id}",
                        json={"name": "改"}).status_code == 403
    assert client.delete(f"/api/v1/templates/{other.id}").status_code == 403