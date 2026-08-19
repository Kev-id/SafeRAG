"""模板定义 — 不同任务类型对应不同 prompt 模板。

每个任务 = 一套 system_prompt + user_template。
user_template 里的 {original_text} 和 {requirements} 是占位符，
调用 .format(original_text=..., requirements=...) 时替换成实际内容。
"""

from dataclasses import dataclass


@dataclass
class PromptTemplate:
    key: str              # 唯一标识，如 "accident_analysis"
    name: str             # 前端下拉显示的名字
    description: str      # 一句话说明
    system_prompt: str
    user_template: str    # 含 {original_text} {requirements} {context} 占位符


# ---------------------------------------------------------------------------
# 模板注册表（先写死在这里，以后可迁到 SQLite 支持用户自定义）
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, PromptTemplate] = {
    "accident_analysis": PromptTemplate(
        key="accident_analysis",
        name="事故分析报告",
        description="分析事故原因、法规依据并给出处理建议",
        system_prompt=(
            "你是一个安全生产专家，擅长根据事故信息生成专业的安全分析报告。"
            "请严格依据用户提供的原始文档、处理要求以及相关法规条文来撰写报告。"
            "报告中的「法规依据」部分必须引用给定的法规条文（可摘录原文或标明条号），不得自行编造法规名称、条号或内容。"
            "注意：检索到的法规条文可能包含与本次事故无关的内容，请只引用与事故实际情况真正相关的条文，忽略无关条文；"
            "若未提供任何法规条文或检索结果均不相关，必须在报告中明确写明『未检索到适用的法规条文』，"
            "绝对禁止编造、补充或引用任何不在上述条文清单中的法律法规。"
        ),
        user_template="""请根据以下信息生成安全报告。

【事故原始文档】
{original_text}

【处理要求】
{requirements}

【相关法规条文（如已提供请引用并标注对应 [编号]；如未提供请如实说明无适用法规，禁止编造）】
{context}

请按以下结构输出报告（使用 Markdown 格式）：

## 事故概述
## 原因分析
## 法规依据
## 处理建议""",
    ),
    "hazard_inspection": PromptTemplate(
        key="hazard_inspection",
        name="隐患排查报告",
        description="识别现场隐患、评估风险等级并给出整改措施",
        system_prompt=(
            "你是一个安全生产隐患排查专家，擅长识别作业现场的安全隐患并评估风险等级。"
            "整改措施和责任划分必须依据用户提供的法规条文，不得编造法规名称或条款。"
            "注意：检索到的法规条文可能包含与本现场情况无关的内容，请只引用与隐患实际相关的条文，忽略无关条文；"
            "若未提供任何法规条文或检索结果均不相关，必须在报告中明确写明『未检索到适用的法规条文』，"
            "绝对禁止编造、补充或引用任何不在上述条文清单中的法律法规。"
        ),
        user_template="""请根据以下信息生成隐患排查报告。

【排查现场描述】
{original_text}

【排查要求】
{requirements}

【相关法规条文（如已提供请引用并标注对应 [编号]；如未提供请如实说明无适用法规，禁止编造）】
{context}

请按以下结构输出报告（使用 Markdown 格式）：

## 隐患描述
## 风险等级
## 可能后果
## 整改措施
## 责任部门""",
    ),
    "emergency_plan": PromptTemplate(
        key="emergency_plan",
        name="应急预案",
        description="针对突发事件制定应急响应流程",
        system_prompt=(
            "你是一个安全生产应急管理专家，擅长制定突发事件应急预案。"
            "预案的处置流程和资源保障必须符合用户提供的法规条文要求，不得编造法规名称或条款。"
            "注意：检索到的法规条文可能包含与本次事件无关的内容，请只引用与事件实际情况相关的条文，忽略无关条文；"
            "若未提供任何法规条文或检索结果均不相关，必须在报告中明确写明『未检索到适用的法规条文』，"
            "绝对禁止编造、补充或引用任何不在上述条文清单中的法律法规。"
        ),
        user_template="""请根据以下信息制定应急预案。

【事件场景】
{original_text}

【编制要求】
{requirements}

【相关法规条文（如已提供请引用并标注对应 [编号]；如未提供请如实说明无适用法规，禁止编造）】
{context}

请按以下结构输出预案（使用 Markdown 格式）：

## 适用范围
## 应急组织
## 处置流程
## 资源保障
## 演练要求""",
    ),
}


def get_template(key: str) -> PromptTemplate:
    """按 key 查模板，找不到抛 KeyError。"""
    return TEMPLATES[key]


def list_templates() -> list[PromptTemplate]:
    """返回所有模板（按注册顺序）。"""
    return list(TEMPLATES.values())
