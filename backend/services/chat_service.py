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
_RAG_SYSTEM_PREFIX = "回答问题时，可以参考以下法规：\n"
_RAG_SYSTEM_SUFFIX = "\n当需要直接引用法条原文或部分内容时，必须一字不差地摘录，禁止任何改写或推测。"

def build_chat_messages(
    messages: list[dict], enable_rag: bool
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

    question = messages[-1].get("content", "") or ""
    sources: list[str] = []

    if enable_rag and question.strip():
        context, sources = retrieve_with_citations(question)
        if context:
            messages = [
                {"role": "system", "content":  context + _RAG_SYSTEM_PREFIX},
                *messages,
            ]
    return messages, sources
