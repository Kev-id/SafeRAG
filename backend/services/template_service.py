"""业务层 — 用户报告模板（templates 表）。

模板 = 有序"节"列表 [{title, instruction}]，编号由索引派生（增删节自动重排）。
owner_id=NULL 是系统示例（全员可见、只读），固定 id = 旧任务类型 key
（accident_analysis 等）：既兼容旧 /api/v1/tasks 与旧 task_type 调用，
又是新模板体系的参考示例。
"""

import logging

from backend.repositories import template_repo
from backend.repositories.template_repo import Template

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 系统示例模板内容（seed 用）
# 事故分析拆成 8 节点式参考；另两套按原 prompt 章节骨架建节，可后续精修。
# 系统提示词已大幅简略（见 report_builder.SYSTEM_SECTION_PROMPT），
# 各节的具体撰写要求由本节的 instruction 承担。
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATES: list[dict] = [
    {
        "id": "accident_analysis",
        "name": "事故分析报告",
        "description": "分析事故原因、法规依据并给出处理建议",
        "sections": [
            {"title": "基本情况", "instruction":
             "把原始文档中的事实抽成清单分项列全：事故单位及相关单位概况、事发时间地点、"
             "事故经过、人员伤亡、直接经济损失、应急处置等。人名、单位名、时间、数字一律照抄原文，"
             "宁多勿漏；原文未记载的信息不补写。"},
            {"title": "原因分析", "instruction":
             "按三层递进归因：直接原因（技术机理层）→间接原因（安全管理层）→根本原因（制度责任层）。"
             "每条归因必须四要素齐全：主体+行为+后果+原文依据；禁止以先后当因果、以伴随当因果、"
             "主体调换、以现象替代机理。删除该原因后事故仍会发生，则只是加重因素，不得列为主要原因。"},
            {"title": "事故性质认定", "instruction":
             "判断责任事故/非责任事故必须要有原文依据；依据不足写『待调查组认定』，不得臆断。"},
            {"title": "问题与责任", "instruction":
             "按企业层面→有关部门层面→地方党委政府层面展开；每一条责任对应原因分析中的一条原因，"
             "不孤立罗列。"},
            {"title": "处理建议", "instruction":
             "区分司法机关已采取强制措施的人员、公职人员处理建议、行政处罚建议三类；"
             "均有原文依据才写，依据不足如实说明。"},
            {"title": "教训", "instruction":
             "从思想、管理、监管、法治层面总结；教训必须对应报告实际写出的问题，禁止通用套话。"},
            {"title": "整改措施", "instruction":
             "针对原因分析的每一条原因给出对策：技术措施、管理措施、制度措施分层，"
             "一条原因对一条措施，可操作、责任到岗。"},
            {"title": "应急处置评估", "instruction":
             "适用于自然灾害或涉及大规模救援的事故，评估应急响应、救援过程与救援效果；"
             "本章不适用时明确写『本章无适用内容』。"},
        ],
    },
    {
        "id": "hazard_inspection",
        "name": "隐患排查报告",
        "description": "识别现场隐患、评估风险等级并给出整改措施",
        "sections": [
            {"title": "排查概况", "instruction":
             "排查时间与范围、排查依据、参加人员、排查方法；原始材料未记载的信息如实说明，不得编造。"},
            {"title": "隐患明细清单", "instruction":
             "逐条输出每条隐患八要素：位置、隐患描述、隐患类别（物的不安全状态/人的不安全行为/"
             "作业环境不良/管理缺陷）、风险等级（一般/重大）及判定理由、可能后果（隐患→可能触发的事故→"
             "后果）、整改建议、整改期限、责任部门及责任人。宁细毋漏，不得合并模糊。"},
            {"title": "重大隐患专项说明", "instruction":
             "逐条说明判定为重大的理由（看危害后果而非只看整改难度）、挂牌督办要求、治理与验收安排；"
             "无重大隐患可整章省略。"},
            {"title": "整改措施汇总与治理闭环", "instruction":
             "按立即整改项→限期整改项→停产停业整改项分类汇总；明确复查、验收、销号的责任与时限，"
             "形成闭环，不留尾巴。"},
            {"title": "跟踪复查安排", "instruction":
             "整改完成后的复查方式与周期；不适用可省略。"},
        ],
    },
    {
        "id": "emergency_plan",
        "name": "应急预案",
        "description": "针对突发事件制定应急响应流程",
        "sections": [
            {"title": "适用范围", "instruction":
             "本预案适用的事故类型、发生区域与对象；说明是综合/专项/现场处置方案中的哪一种。"},
            {"title": "事故风险描述", "instruction":
             "从事件场景提取风险源与危险物质及其所在部位、危险特性、可能后果、影响范围、"
             "现场可用应急资源（器材/队伍/联络方式）。"},
            {"title": "应急组织机构及职责", "instruction":
             "总指挥、现场指挥，各应急小组（抢险救援/医疗救护/疏散警戒/后勤保障/通信联络）及其职责；"
             "每组职责写清在响应中的哪个阶段做什么；关键岗位设 AB 角。"},
            {"title": "预警与信息报告", "instruction":
             "风险监测与预警条件；险情报告流程（报告给谁、时限、报告内容）；报警电话与内部值班电话。"},
            {"title": "应急响应", "instruction":
             "响应分级及各级启动条件与授权人。处置程序按先期处置→报警上报→启动预案→各组展开→"
             "险情扩大升级增援→后期处置递进，上下动作顺序不可颠倒，写明在什么条件下做、谁做、做什么；"
             "处置措施逐条对应第二章风险源。"},
            {"title": "处置纪律与注意事项", "instruction":
             "按本场景类型逐条写（触电/火灾/受限空间/危化品泄漏/爆炸坍塌/高处等），不可跨类型套用。"},
            {"title": "应急保障", "instruction":
             "队伍、装备物资（器材种类数量与存放位置）、通信、经费保障；无法落实的资源如实写明"
             "『需另行配置』，不编造。"},
            {"title": "后期处置", "instruction":
             "现场清理与恢复、伤亡善后、事故调查配合、预案演练与修订要求。"},
        ],
    },
]


def seed_system_templates() -> None:
    """幂等 seed 三套系统示例模板（缺谁建谁）。启动时调用一次。"""
    for t in _SYSTEM_TEMPLATES:
        if template_repo.get(t["id"]) is None:
            template_repo.create(
                name=t["name"], description=t["description"],
                sections=t["sections"], owner_id=None, template_id=t["id"],
            )
            logger.info("已创建系统示例模板: %s (%d 节)", t["id"], len(t["sections"]))


def list_templates() -> list[Template]:
    """全部系统示例（旧 /api/v1/tasks 兼容）。"""
    return template_repo.list_system()


def get_template(task_key: str) -> Template:
    """按 key 取模板（兼容旧调用：指针只指向系统示例）。不存在抛 KeyError。"""
    t = template_repo.get(task_key)
    if t is None or not t.is_system:
        raise KeyError(task_key)
    return t


def list_mine(user_id: int) -> list[Template]:
    """该用户可见模板：系统示例 + 自己的。"""
    return template_repo.list_mine(user_id)


def get_visible(user_id: int, template_id: str) -> Template:
    """取模板并校验用户可见（系统或自己的），否则抛错误。"""
    t = template_repo.get(template_id)
    if t is None:
        raise KeyError(template_id)
    if not t.is_system and t.owner_id != user_id:
        raise ValueError("无权使用该模板")
    return t


def resolve_template(template_id: str, task_type: str, user_id: int) -> Template:
    """解析提交时使用的模板：template_id 优先，否则退旧 task_type（仅系统 key）。"""
    if template_id:
        return get_visible(user_id, template_id)
    if task_type:
        return get_template(task_type)   # KeyError → 请求层 422
    raise KeyError("未指定模板")


def create(name: str, description: str, sections: list[dict], owner_id: int) -> Template:
    """新建用户自己的模板。"""
    return template_repo.create(name=name, description=description,
                                sections=sections, owner_id=owner_id)


def update(template_id: str, user_id: int, *,
           name: str | None = None, description: str | None = None,
           sections: list[dict] | None = None) -> Template:
    """编辑自己的模板；系统示例只读（复制后再改）。"""
    t = template_repo.get(template_id)
    if t is None:
        raise KeyError(template_id)
    if t.is_system:
        raise ValueError("系统示例模板只读，请复制后再改")
    if t.owner_id != user_id:
        raise ValueError("只能编辑自己的模板")
    updated = template_repo.update(template_id, name=name, description=description, sections=sections)
    return updated


def delete(template_id: str, user_id: int) -> bool:
    """删除自己的模板；系统示例不能删。"""
    t = template_repo.get(template_id)
    if t is None:
        raise KeyError(template_id)
    if t.is_system:
        raise ValueError("系统示例模板只读，不能删除")
    if t.owner_id != user_id:
        raise ValueError("只能删除自己的模板")
    return template_repo.delete(template_id)