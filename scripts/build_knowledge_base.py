"""知识库构建脚本 — 读取法规条文，向量化后存入 ChromaDB。

用法:
    python scripts/build_knowledge_base.py [--source rag.txt]

流程:
    读源文件（每行一条法规）→ BGE 向量化 → 存 ChromaDB（{KB_DIR}）

说明:
    ChromaDB 只负责「向量语义检索」。关键词检索（BM25）在检索阶段
    用 rank_bm25 另做，两者结果用 RRF 融合。详见检索模块。
"""

import argparse
import logging
import os
import sys

# 保证 `python scripts/build_knowledge_base.py` 直接跑也能 import backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chromadb

from backend.core.config import KB_COLLECTION, KB_DIR
from backend.core.embedding_client import BgeEmbeddingFunction

logger = logging.getLogger(__name__)


def load_lines(path: str) -> list[str]:
    """读取源文件，每行一条法规，过滤空行和纯空白行。"""
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            text = raw.strip()
            if text:
                lines.append(text)
    return lines


def build(source_path: str, persist_dir: str = KB_DIR, collection_name: str = KB_COLLECTION) -> None:
    """构建知识库：读行 → 向量化 → 存 ChromaDB。"""
    lines = load_lines(source_path)
    if not lines:
        raise ValueError(f"源文件为空或不存在: {source_path}")

    client = chromadb.PersistentClient(path=persist_dir)

    # 先删旧 collection 再重建，保证脚本可重复运行（幂等）
    try:
        client.delete_collection(collection_name)
        logger.info("已删除旧 collection '%s'", collection_name)
    except Exception:
        pass

    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=BgeEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"},
    )

    ids = [f"law-{i:04d}" for i in range(1, len(lines) + 1)]
    metadatas = [
        {"line": i, "source": os.path.basename(source_path)}
        for i in range(1, len(lines) + 1)
    ]

    batch = 32
    for start in range(0, len(lines), batch):
        end = min(start + batch, len(lines))
        collection.add(
            ids=ids[start:end],
            documents=lines[start:end],
            metadatas=metadatas[start:end],
        )
        logger.info("已写入 %d/%d 条", end, len(lines))

    print(f"\n✅ 知识库构建完成: {len(lines)} 条 → collection '{collection_name}' @ {persist_dir}")

    # 自测：查一条，验证端到端能检索
    test_q = "企业应当如何开展安全生产教育和培训？"
    res = collection.query(query_texts=[test_q], n_results=3)
    print(f"\n自测查询: {test_q}")
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        print(f"  [第{meta['line']}行] (距离={dist:.4f}) {doc[:40]}...")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

    parser = argparse.ArgumentParser(description="构建 RAG 知识库（法规条文 → ChromaDB）")
    parser.add_argument("--source", default="rag.txt", help="源文件路径，每行一条法规")
    args = parser.parse_args()

    build(args.source)