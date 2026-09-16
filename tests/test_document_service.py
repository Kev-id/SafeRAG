"""document_service 逐节流水线 + 单节精修 集成测试（mock LLM 与检索）。

在 conda SafeRAG / CI 环境跑（document_service 顶层 import retriever → jieba）。
隔离：DATABASE_URL 与 DATA_DIR 都指向临时目录，不碰生产库/报告文件。
"""

import asyncio
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
_TMP_DATA = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"
os.environ["DATA_DIR"] = _TMP_DATA

import pytest  # noqa: E402

from backend.core.database import init_db  # noqa: E402
from backend.repositories.document_repo import DocStatus, Document, save  # noqa: E402
from backend.services import document_service as ds  # noqa: E402
from backend.services import template_service  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    init_db()
    template_service.seed_system_templates()
    yield


def _doc(snapshot=None, task_type="", status=DocStatus.QUEUED) -> Document:
    doc = Document(
        original_text="事故描述",
        requirements="要求",
        output_filename="报告",
        task_type=task_type,
        template_id=task_type,
        template_snapshot=snapshot or [],
        status=status,
    )
    save(doc)
    return doc


def _ok_chat(seq):
    """按序返回的结果；取尽后抛错。"""
    calls = {"n": 0}

    async def fake(messages):
        if calls["n"] >= len(seq):
            raise RuntimeError("mock 结果耗尽")
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    return fake


def _fail_chat(seq):
    """返回 seq 中前面几条，随后抛错（模拟单节生成失败）。"""
    calls = {"n": 0}

    async def fake(messages):
        if calls["n"] >= len(seq):
            raise RuntimeError("mock 生成失败")
        r = seq[calls["n"]]
        calls["n"] += 1
        return r

    return fake


def _fake_retrieve(context="法规上下文", sources=None):
    def fake(*a, **k):
        return context, sources or []

    return fake


def test_process_document_sequential_sections_and_report(monkeypatch):
    doc = _doc(snapshot=[{"title": "甲", "instruction": "i1"}, {"title": "乙", "instruction": "i2"}])
    monkeypatch.setattr(ds, "qwen_chat", _ok_chat(["一内容", "二内容"]))
    monkeypatch.setattr(ds, "retrieve_with_citations",
                        _fake_retrieve("法规", ["[1] a.txt 第一条"]))

    asyncio.run(ds._process_document(doc))

    assert doc.status == DocStatus.COMPLETED
    assert [s["status"] for s in doc.sections] == ["completed", "completed"]
    assert doc.sections[0]["content"] == "一内容"
    assert "## 一、甲" in doc.report_content and "一内容" in doc.report_content
    assert "## 参考法规来源" in doc.report_content
    assert "[1] a.txt 第一条" in doc.report_content


def test_process_document_section_failure_continues(monkeypatch):
    doc = _doc(snapshot=[{"title": "甲", "instruction": "i"}, {"title": "乙", "instruction": "i"}])
    monkeypatch.setattr(ds, "qwen_chat", _fail_chat(["一内容"]))   # 第二节失败
    monkeypatch.setattr(ds, "retrieve_with_citations", _fake_retrieve())

    asyncio.run(ds._process_document(doc))

    assert doc.status == DocStatus.COMPLETED          # 至少一节成功 → 整篇成功
    assert doc.sections[0]["status"] == "completed"
    assert doc.sections[1]["status"] == "failed"
    assert "一内容" in doc.report_content
    assert "## 二、乙" not in doc.report_content      # 失败节不渲染


def test_process_document_all_failed(monkeypatch):
    doc = _doc(snapshot=[{"title": "甲", "instruction": "i"}])
    monkeypatch.setattr(ds, "qwen_chat", _fail_chat([]))
    monkeypatch.setattr(ds, "retrieve_with_citations", _fake_retrieve())

    asyncio.run(ds._process_document(doc))

    assert doc.status == DocStatus.FAILED


def test_process_document_empty_snapshot_fails(monkeypatch):
    doc = _doc(snapshot=[])
    monkeypatch.setattr(ds, "retrieve_with_citations", _fake_retrieve())

    asyncio.run(ds._process_document(doc))

    assert doc.status == DocStatus.FAILED


def test_legacy_task_type_falls_back_to_system_template(monkeypatch):
    """旧文档（task_type 无快照）→ 兜底成系统模板逐节。"""
    doc = _doc(task_type="accident_analysis")
    monkeypatch.setattr(ds, "qwen_chat", _ok_chat([f"节{i}内容" for i in range(8)]))
    monkeypatch.setattr(ds, "retrieve_with_citations", _fake_retrieve())

    asyncio.run(ds._process_document(doc))

    assert doc.status == DocStatus.COMPLETED
    assert len(doc.sections) == 8
    assert doc.template_id == "accident_analysis"


def test_revise_section_updates_only_that_section(monkeypatch):
    doc = _doc(snapshot=[{"title": "甲", "instruction": "i1"}, {"title": "乙", "instruction": "i2"}])
    monkeypatch.setattr(ds, "qwen_chat", _ok_chat(["原甲", "原乙"]))
    monkeypatch.setattr(ds, "retrieve_with_citations", _fake_retrieve())
    asyncio.run(ds._process_document(doc))

    monkeypatch.setattr(ds, "qwen_chat", _ok_chat(["精修后的甲"]))
    asyncio.run(ds.revise_section(doc.id, 0, "补充要求", "补充材料"))

    updated = ds.get(doc.id)
    assert updated.sections[0]["content"] == "精修后的甲"
    assert updated.sections[0]["status"] == "completed"
    assert updated.sections[0].get("revised_at")
    assert updated.sections[1]["content"] == "原乙"       # 其它节不动
    assert "精修后的甲" in updated.report_content


def test_revise_section_failure_restores_status_and_content(monkeypatch):
    doc = _doc(snapshot=[{"title": "甲", "instruction": "i"}])
    monkeypatch.setattr(ds, "qwen_chat", _ok_chat(["原甲"]))
    monkeypatch.setattr(ds, "retrieve_with_citations", _fake_retrieve())
    asyncio.run(ds._process_document(doc))
    old_content = doc.sections[0]["content"]

    monkeypatch.setattr(ds, "qwen_chat", _fail_chat([]))
    with pytest.raises(RuntimeError):
        asyncio.run(ds.revise_section(doc.id, 0, "x", "y"))

    got = ds.get(doc.id)
    assert got.sections[0]["status"] == "completed"       # 恢复原状态
    assert got.sections[0]["content"] == old_content      # 旧内容保留