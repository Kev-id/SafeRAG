"""RAG 排查脚本 — 打印「检索 → 拼 prompt」每一层，不调 Qwen。

用法:
    python scripts/debug_rag.py ["事故描述文本"]

作用:
    定位「法规到底有没有进 prompt」的问题。三层输出：
      ① 检索器原始命中（id / 条文 / RRF 得分）
      ② _retrieve_context 的返回值（服务里真实注入用的就是它）
      ③ 拼出的完整 user prompt（= 实际发给模型的文字）

怎么读结果:
    - ① 就报错 / 空 → 知识库没建好，先跑 build_knowledge_base.py
    - ① 有结果但条文和事故不相关 → 检索 query 有问题（很可能用了整篇原文当 query）
    - ① 有、② 空 → 中间拼接断了
    - ③ 有法规但模型生成时不用 → 模型/prompt 侧问题
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.retriever import get_retriever
from backend.services.document_service import _build_messages, _retrieve_context
from backend.services.template_service import get_template


def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else "某工厂进行高处作业时发生坠落事故，一名工人受伤。"

    print("① 检索器原始命中")
    print("-" * 60)
    try:
        hits = get_retriever().retrieve(text, top_k=5)
    except Exception as e:
        print(f"  ❌ 检索器加载/查询失败: {e}")
        print("  → 知识库没建好，先跑: python scripts/build_knowledge_base.py")
        return
    if not hits:
        print("  ❌ 检索返回空（知识库是空的，或 query 和条文完全不搭）")
    for h in hits:
        print(f"  [{h['meta'].get('source')}#{h['meta'].get('chunk')}] RRF={h['score']}")
        print(f"     {h['text'][:80]}")

    print()
    print("② _retrieve_context 返回值（服务里注入用的就是它）")
    print("-" * 60)
    context, sources = _retrieve_context(text, top_k=5)
    print(context if context.strip() else "  ❌ 空串 —— _retrieve_context 降级了")
    print()
    print("   —— 报告末尾会追加的「参考法规来源」清单 ——")
    for s in sources:
        print(f"     {s}")

    print()
    print("③ 拼出的完整 user prompt（= 发给模型的文字）")
    print("-" * 60)
    template = get_template("accident_analysis")
    messages = _build_messages(template, text, "请分析原因并给出处理建议", context)
    print(messages[1]["content"])


if __name__ == "__main__":
    main()
