"""template_service 的单元测试 — 纯逻辑，零外部依赖。

这是全项目最"纯"的一层：输入确定 → 输出确定，
不碰数据库、不碰网络、不写文件，所以测试最简单，适合作为第一个样例。

每个测试函数只验证一件事，函数名用 test_ 开头，
pytest 会自动发现并运行它们。
"""

import pytest

from backend.services.template_service import get_template, list_templates


def test_get_template_returns_correct_template():
    """正确的 key 返回正确的模板。"""
    t = get_template("accident_analysis")
    assert t.name == "事故分析报告"
    assert "【输出格式要求】" in t.user_template


def test_get_template_unknown_key_raises():
    """未知的 key 应该抛 KeyError。"""
    with pytest.raises(KeyError):
        get_template("不存在的任务类型")


def test_list_templates_returns_all_three():
    """list 应该返回全部 3 个模板。"""
    templates = list_templates()
    assert len(templates) == 3
    keys = {t.key for t in templates}
    assert keys == {"accident_analysis", "hazard_inspection", "emergency_plan"}


def test_every_template_has_placeholders():
    """每个模板都必须含三个占位符，否则 _build_messages 的 .format() 会崩。

    这是"数据完整性"测试：模板是手写数据，最容易漏占位符。
    以后你加新模板，这个测试会自动帮你检查。
    """
    for t in list_templates():
        assert "{original_text}" in t.user_template, f"{t.key} 缺少 {{original_text}}"
        assert "{requirements}" in t.user_template, f"{t.key} 缺少 {{requirements}}"
        assert "{context}" in t.user_template, f"{t.key} 缺少 {{context}}"
        assert t.system_prompt, f"{t.key} 的 system_prompt 为空"
