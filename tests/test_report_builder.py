"""report_builder 纯函数测试 — 逐节消息拼接、前序截断、报告渲染。

本模块不 import retriever/qwen，可在本机（无 jieba）直接跑。
"""

from backend.services import report_builder as rb


def test_build_section_messages_carries_instruction_and_previous():
    sections = [
        {"title": "基本情况", "instruction": "列事实", "content": "已生成的第一节内容", "status": "completed"},
        {"title": "原因分析", "instruction": "三层归因", "content": None, "status": "queued"},
    ]
    msgs = rb.build_section_messages("事故原文", "总体要求", "法规", sections, 1, 1000)

    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "当前需撰写章节：二、原因分析（第 2/2 章）" in user
    assert "三层归因" in user                        # 本节 instruction
    assert "已生成的第一节内容" in user              # 前序章节供衔接
    assert "原文" in user and "总体要求" in user


def test_trim_previous_keeps_newest_within_budget():
    prev = ["A" * 800, "B" * 800, "C" * 800]
    out1 = rb.trim_previous(prev, 1500)
    assert out1 == "C" * 800               # 只装得下最近一节
    out2 = rb.trim_previous(prev, 2000)
    assert out2 == "B" * 800 + "\n\n" + "C" * 800   # 最近两节，弃最旧


def test_render_report_numbering_and_sources():
    sections = [
        {"title": "甲", "content": "第一段正文"},
        {"title": "乙", "content": ""},                 # 空节不渲染（不产生"二、乙"）
        {"title": "丙", "content": "第三段正文"},
    ]
    out = rb.render_report("我的报告", sections, ["[1] 来源 第一条"])

    assert out.startswith("# 我的报告")
    assert "\n\n## 一、甲\n第一段正文" in out
    assert "## 二、乙" not in out
    assert "\n\n## 三、丙\n第三段正文" in out
    assert "## 参考法规来源\n\n[1] 来源 第一条" in out


def test_build_revise_messages_has_extra_and_others():
    sections = [
        {"title": "甲", "instruction": "i1", "content": "旧甲"},
        {"title": "乙", "instruction": "i2", "content": "旧乙"},
    ]
    msgs = rb.build_revise_messages("原文", "要求", "", sections, 0, "补充要求文本", "补充材料文本", 1000)
    user = msgs[1]["content"]

    assert "需改进章节：一、甲" in user
    assert "用户本次补充要求：\n补充要求文本" in user
    assert "补充材料文本" in user
    assert "旧乙" in user               # 其它章节作上下文
    assert "用户本次补充要求" in user