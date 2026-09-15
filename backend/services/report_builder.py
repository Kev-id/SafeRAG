"""纯函数 — 逐节报告的 prompt 拼接 / 前序截断 / .md 渲染。

故意不 import retriever / qwen_client：本模块只做字符串与结构的拼装，
可在本机（无 jieba）直接单测，真正的 IO 编排在 document_service。

章节编号从模板节索引派生（template_repo.section_no），增删节自动重排。
"""

from backend.repositories.template_repo import section_no

# 逐节生成用的 system 提示：比旧"治理四件套"大 Prompt 简略——章节要求由各节 instruction 承担
SYSTEM_SECTION_PROMPT = (
    "你是安全生产与公文撰写专家。你只负责撰写报告当前指定的一章：只输出本章正文，"
    "不要输出其他章节、不要输出标题、不要加任何说明。引用法规须标注 [编号]；"
    "本章无适用法规则明确写『本章无适用法规』。"
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
    """构造"单节精修重生成"的 messages：针对 index 节，追加本次要求/资料。"""
    sec = sections[index]
    no = section_no(index)
    title = sec.get("title", "")
    instruction = sec.get("instruction", "")

    # 其它章节作上下文（含本人旧内容，模型可对照改进，不含待生成节）
    others: list[str] = []
    for i, s in enumerate(sections):
        if i == index or not s.get("content"):
            continue
        others.append(f"{section_no(i)}{s.get('title','')}\n{s['content']}")
    others_text = trim_previous(others, budget)

    user = (
        f"原始文档：\n{original_text}\n\n"
        f"总体要求：\n{requirements}\n\n"
        f"相关法规条文（有则引用并标注 [编号]，无则忽略）：\n{context or '（未注入法规）'}\n\n"
        f"需改进章节：{no}{title}\n"
        f"本章原撰写要求：\n{instruction}\n\n"
        f"用户本次补充要求：\n{extra_requirements or '（无）'}\n\n"
        f"用户补充材料（结合进本章，勿照抄，勿编造材料外的数字/事实）：\n{materials or '（无）'}\n\n"
        f"其它章节（供上下文衔接，勿重复，勿改动它们）：\n{others_text}\n\n"
        f"请只输出本章改进后的正文。"
    )
    return [
        {"role": "system", "content": SYSTEM_SECTION_PROMPT},
        {"role": "user", "content": user},
    ]


def render_report(title: str, sections: list[dict], sources: list[str] | None) -> str:
    """从节拼回整篇 .md：'# 标题' + 每节 '## 一、{title}' + 正文；末尾附「参考法规来源」。

    只收 content 非空的节；编号由索引派生，增删节后自动重排。
    """
    lines = [f"# {title}"]
    for i, sec in enumerate(sections):
        content = (sec.get("content") or "").strip()
        if not content:
            continue
        lines += ["", f"## {section_no(i)}{sec.get('title','')}", content]
    if sources:
        # 每条来源用空行隔开：单 \n 在 Markdown 里是"软换行"（同一段），
        # pandoc 转 Word 会粘连成一段；\n\n 才各自成独立段落
        lines += ["", "---", "## 参考法规来源", "", "\n\n".join(sources)]
    return "\n".join(lines)