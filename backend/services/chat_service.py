"""业务层 — 流式聊天。

把前端从"直连 Qwen SSE"改成"经 SafeRAG 的 SSE 代理"：收到聊天请求后，
按 enable_rag 决定要不要检索法规并注入 system 提示，再调 Qwen 流式透传。

检索复用 document_service.retrieve_with_citations（不依赖文档模板），
失败降级为不注入，纯 LLM 生成。

调用约定：build_chat_messages 在路由处理函数里同步执行（或 to_thread），
抛错发生在 StreamingResponse 创建之前 → 可转 422/503；只有 Qwen 流式透传
放进 SSE 生成器。
"""

import logging

from backend.services.document_service import retrieve_with_citations

logger = logging.getLogger(__name__)

# 注入 system 的提示词头：告诉模型参考法规并用 [编号] 标注
_RAG_SYSTEM_PREFIX = "回答问题时，可以参考以上法规：\n当需要直接引用法条原文或部分内容时，必须一字不差地摘录，禁止任何改写或推测。\n"


def _content_to_text(content) -> str:
    """取消息文本部分：content 可能是 str，也可能是 OpenAI 多模态数组
    [{type:text},{type:image_url}]。图片项不参与关键词检索，只取 text。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            (item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ).strip()
    return ""


def build_chat_messages(
    messages: list[dict], enable_rag: bool, region: str | None = None
) -> tuple[list[dict], list[str]]:
    """构造发给 Qwen 的 messages。

    取最后一轮 user 文本做检索（enable_rag 时）；检索到法规 → 在最前插一条
    独立 system（带编号法规）；不开/失败 → 原样转发。

    返回 (constructed_messages, sources)：sources 是 [编号]→来源 清单，
    供上层（若需要）拼"参考法规来源"附录。

    抛 ValueError（messages 为空）。检索失败被 retrieve_with_citations 吞掉，
    降级为不注入，不抛错。
    """
    if not messages:
        raise ValueError("messages 不能为空")

    question = _content_to_text(messages[-1].get("content", ""))
    sources: list[str] = []

    if enable_rag and question.strip():
        context, sources = retrieve_with_citations(question, region=region)
        if context:
            rag_content = context + _RAG_SYSTEM_PREFIX
            # Qwen 聊天模板要求 system 只能有一条且在开头（否则模板报
            # "System message must be at the beginning"）：
            # 若 messages 已以 system 开头 → 把 RAG 内容并进去（保持 index 0）；
            # 否则 → 在最前面插一条独立 system。
            if messages and messages[0].get("role") == "system":
                messages = [
                    {"role": "system", "content": rag_content + "\n\n" + messages[0]["content"]},
                    *messages[1:],
                ]
            else:
                messages = [{"role": "system", "content": rag_content}, *messages]
    return messages, sources
