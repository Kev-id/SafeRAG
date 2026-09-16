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
    assert "【输出格式要求】" in msgs[0]["content"]        # 格式规范只放 system 一处
    assert "## 一、基本情况" in msgs[0]["content"]         # 章标题由模型自带
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


def test_render_report_no_duplicate_when_model_wrote_heading():
    """模型按格式要求自带『## 一、…』标题 → 渲染器不再重复加。"""
    sections = [{"title": "甲", "content": "## 一、甲\n正文内容"}]
    out = rb.render_report("我的报告", sections, None)
    assert out.count("## 一、甲") == 1
    assert "\n\n## 一、甲\n正文内容" in out


def test_render_report_adds_heading_if_model_omitted():
    """模型漏写标题 → 渲染器补一个（防缺标题）。"""
    sections = [{"title": "甲", "content": "只有正文没有标题"}]
    out = rb.render_report("我的报告", sections, None)
    assert "\n\n## 一、甲\n只有正文没有标题" in out


def test_build_revise_messages_bases_on_own_section():
    """精修只基于本节自己内容（+补充要求/材料），不带其它章节。"""
    sections = [
        {"title": "甲", "instruction": "i1", "content": "旧甲（本节原内容）"},
        {"title": "乙", "instruction": "i2", "content": "旧乙"},
    ]
    msgs = rb.build_revise_messages("原文", "要求", "", sections, 0, "补充要求文本", "补充材料文本", 1000)
    user = msgs[1]["content"]

    assert "需改进章节：一、甲" in user
    assert "一、甲\n旧甲（本节原内容）" in user      # 注入本节自己，作改进底稿
    assert "用户本次补充要求（最高优先级" in user     # 补充要求是硬约束（防被模板结构带偏）
    assert "补充要求文本" in user
    assert "补充材料文本" in user
    assert "只保留（一）" in user or "只保留某部分" in user  # 范围纪律措辞
    assert "旧乙" not in user                        # 其它章节不再注入
    assert "（未注入法规）" in user                  # 精修当前不注入法规，明确提示防幻觉