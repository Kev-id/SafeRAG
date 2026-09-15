"""template_service / template_repo 单元测试 — seed 幂等、编号派生、系统只读、用户归属。

需要临时 SQLite（import database 前设 DATABASE_URL），不碰生产 saferag.db。
"""

import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

import pytest  # noqa: E402

from backend.core.database import init_db  # noqa: E402
from backend.repositories import template_repo  # noqa: E402
from backend.services import template_service  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    init_db()
    template_service.seed_system_templates()
    yield


def test_seed_three_system_templates():
    ts = template_service.list_templates()
    assert len(ts) == 3
    assert {t.id for t in ts} == {"accident_analysis", "hazard_inspection", "emergency_plan"}
    acc = template_service.get_template("accident_analysis")
    assert acc.name == "事故分析报告"
    assert acc.is_system
    assert len(acc.sections) == 8                      # 事故分析拆成 8 节
    assert all("title" in s and "instruction" in s for s in acc.sections)


def test_seed_idempotent():
    template_service.seed_system_templates()
    assert len(template_service.list_templates()) == 3


def test_section_no_derived_numbering():
    """编号由索引派生，删节后自动重排（序号不残留）。"""
    assert template_repo.section_no(0) == "一、"
    assert template_repo.section_no(1) == "二、"
    assert template_repo.section_no(9) == "十、"
    assert template_repo.section_no(10) == "11、"
    # 删掉第一节后，剩下的在索引 0 位上重排为"一、"（不复用被删节的号）
    acc = template_service.get_template("accident_analysis")
    rest = acc.sections[1:]
    assert template_repo.section_no(0) + rest[0]["title"] == "一、" + rest[0]["title"]


def test_system_template_readonly():
    with pytest.raises(ValueError):
        template_service.update("accident_analysis", user_id=1, name="改")
    with pytest.raises(ValueError):
        template_service.delete("accident_analysis", user_id=1)


def test_user_template_ownership():
    u1, u2 = 1, 2
    t = template_service.create("我的模板", "desc", [{"title": "甲", "instruction": "列事实"}], u1)
    assert t.owner_id == u1 and not t.is_system

    # 本人可见可用；他人不可见
    assert any(x.id == t.id for x in template_service.list_mine(u1))
    assert not any(x.id == t.id for x in template_service.list_mine(u2))
    with pytest.raises(ValueError):
        template_service.get_visible(u2, t.id)
    with pytest.raises(ValueError):
        template_service.update(t.id, u2, name="x")
    with pytest.raises(ValueError):
        template_service.delete(t.id, u2)

    # 本人可改可删
    template_service.update(t.id, u1, name="改名")
    assert template_service.get_visible(u1, t.id).name == "改名"
    template_service.delete(t.id, u1)
    with pytest.raises(KeyError):
        template_service.get_visible(u1, t.id)


def test_resolve_template_by_id_and_legacy_task_type():
    assert template_service.resolve_template("", "accident_analysis", 1).id == "accident_analysis"
    assert template_service.resolve_template("accident_analysis", "", 1).id == "accident_analysis"
    with pytest.raises(KeyError):
        template_service.resolve_template("", "不存在的key", 1)
    with pytest.raises(KeyError):
        template_service.resolve_template("", "", 1)
    with pytest.raises(KeyError):
        template_service.resolve_template("不存在的id", "", 1)