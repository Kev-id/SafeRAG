"""纯函数 — 逐节报告的 prompt 拼接 / 前序截断 / .md 渲染。

故意不 import retriever / qwen_client：本模块只做字符串与结构的拼装，
可在本机（无 jieba）直接单测，真正的 IO 编排在 document_service。

章节编号从模板节索引派生（template_repo.section_no），增删节自动重排。
"""

import re

from backend.repositories.template_repo import section_no

# 逐节生成用的 system 提示：章节要求由各节 instruction 承担；格式规范（旧模板【输出格式要求】）直接内嵌 system，
# 只此一处，模型按它输出『## 一、…』章标题与『### （一）…』二级要点，渲染器据此去重。
SYSTEM_SECTION_PROMPT = (
    "你是安全生产与公文撰写专家，只撰写报告当前指定的一章。"
    "只输出本章内容，不要输出其他章节，不要加『以下是正文』之类的说明。"
    "引用法规须标注 [编号]；本章无适用法规则明确写『本章无适用法规』。\n"
    "【输出格式要求】\n"
    "1. 本章用 Markdown 标题：以『## 一、基本情况』作为本章标题（编号与标题用上文给定的当前章节）；"
    "本章内部二级要点用『### （一）…』，至多到三级。\n"
    "2. 每个自然段独立成行，段落之间空一行。\n"
    "3. 正文不使用 #、*、-、> 等 Markdown 符号（标题除外）。\n"
    "4. 不确定信息写『待核实/需补充』；原始材料未记载的信息写『原文未记载』，"
    "禁止编造事实、数字、法规条文。"
)


def trim_previous(previous: list[str], budget: int) -> str:
    """把前序章节输出截断到 budget 字符内（保新弃旧），返回拼接文本。

    previous 按已生成顺序传入；保留最近几章，超出预算丢弃最旧。
    """
    parts: list[str] = []
    total = 0
    for block in reversed(previous):
        if total and total + len(block) > budget:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(reversed(parts)) if parts else "（无前一章）"


def build_section_messages(
    original_text: str,
    requirements: str,
    context: str,
    sections: list[dict],
    index: int,
    budget: int,
) -> list[dict]:
    """构造"逐节生成"这一章的 messages。

    sections = 完整节列表（含已生成节的 content；本轮只针对 index 那一节）。
    返回 [system, user]。
    """
    sec = sections[index]
    no = section_no(index)
    title = sec.get("title", "")
    instruction = sec.get("instruction", "")

    previous: list[str] = []
    for i, s in enumerate(sections[:index]):
        if s.get("content"):
            previous.append(f"{section_no(i)}{s.get('title','')}\n{s['content']}")
    prev_text = trim_previous(previous, budget)

    user = (
        f"原始文档：\n{original_text}\n\n"
        f"总体要求：\n{requirements}\n\n"
        f"相关法规条文（有则引用并标注 [编号]，无则忽略）：\n{context or '（未注入法规）'}\n\n"
        f"当前需撰写章节：{no}{title}（第 {index + 1}/{len(sections)} 章）\n"
        f"本章撰写要求：\n{instruction}\n\n"
        f"前序已生成章节（供衔接与引用，勿重复输出）：\n{prev_text}\n\n"
        "请只输出本章正文。"
    )
    return [
        {"role": "system", "content": SYSTEM_SECTION_PROMPT},
        {"role": "user", "content": user},
    ]


def build_revise_messages(
    original_text: str,
    requirements: str,
    context: str,
    sections: list[dict],
    index: int,
    extra_requirements: str,
    materials: str,
    budget: int,
) -> list[dict]:
    """构造"单节精修"的 messages：仅基于本节当前内容 + 本次追加要求/材料改进。

    （用户拍板 2026-09-15）精修只"看自己这一节"：注入本节现有内容作可修改底稿，
    不带其它章节，也不重新检索法规（context 调方传 ""，显示"未注入法规"防幻觉）。

    幂等/守约关键：**用户本次补充要求是硬约束**——把它放在底稿与原要求之前，
    并显式声明"可覆盖原要求、范围/增删以此为准"；否则模型会被模板原要求（如
    "必须(一)(二)(三)递进"）拉着补全完整结构，纵使要求"只保留(一)"也照样罗列。
    budget 参数保留以兼容签名；因不再注入其它章节，此处不适用。
    """
    sec = sections[index]
    no = section_no(index)
    title = sec.get("title", "")
    instruction = sec.get("instruction", "")

    own = (sec.get("content") or "").strip()
    own_block = f"{no}{title}\n{own}" if own else "（本节尚无内容）"

    user = (
        f"原始文档：\n{original_text}\n\n"
        f"总体要求：\n{requirements}\n\n"
        f"相关法规条文（有则引用并标注 [编号]，无则忽略）：\n{context or '（未注入法规）'}\n\n"
        f"需改进章节：{no}{title}\n"
        "用户本次补充要求（最高优先级，严格照办；可覆盖本章原要求，范围/增删以此为准）：\n"
        f"{extra_requirements or '（无）'}\n\n"
        f"用户补充材料（结合进本节，勿照抄，勿编造材料外的数字/事实）：\n{materials or '（无）'}\n\n"
        f"本章原撰写要求（次要，仅当与上面补充要求不冲突时参考）：\n{instruction}\n\n"
        "本节当前内容（仅作可修改底稿：按补充要求截取出要保留的部分，"
        "底稿结构与完整度不强制保留）：\n"
        f"{own_block}\n\n"
        "请只输出按用户补充要求调整后的本节正文。若补充要求说只保留某部分"
        "（如只保留（一）），就只输出该部分，直接删除其余，不要补全（二）至（八）等"
        "原结构中的其它内容；不要输出标题以外的任何说明。"
    )
    return [
        {"role": "system", "content": SYSTEM_SECTION_PROMPT},
        {"role": "user", "content": user},
    ]


def render_report(title: str, sections: list[dict], sources: list[str] | None) -> str:
    """从节拼回整篇 .md：'# 标题' + 每节内容 + 末尾「参考法规来源」。

    章节标题由模型按【输出格式要求】自带（`## 一、…`）；这里只在模型漏写标题时
    补一个（防缺标题），绝不在已有标题时重复加（防『## 一、基本情况』下面再叠一行）。
    """
    lines = [f"# {title}"]
    for i, sec in enumerate(sections):
        content = (sec.get("content") or "").strip()
        if not content:
            continue
        no = section_no(i)
        expect = f"{no}{sec.get('title','')}"
        first = content.splitlines()[0].strip()
        # 模型自带『## 一、…』（或裸『一、…』）→ 直接用；否则渲染器补。
        # 注意别用 f-string 拼正则：{1,6} 会被 f-string 求值成元组，正则失效。
        if re.match(r"^#{1,6}\s*" + re.escape(expect) + r"$", first) or first == expect:
            lines += ["", content]
        else:
            lines += ["", f"## {expect}", content]
    if sources:
        # 每条来源用空行隔开：单 \n 在 Markdown 里是"软换行"（同一段），
        # pandoc 转 Word 会粘连成一段；\n\n 才各自成独立段落
        lines += ["", "---", "## 参考法规来源", "", "\n\n".join(sources)]
    return "\n".join(lines)