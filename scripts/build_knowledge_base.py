"""知识库构建脚本 — 扫描目录下所有 txt，按段落切块后存入 ChromaDB。

用法:
    python scripts/build_knowledge_base.py [--source-dir DIR]

流程:
    扫描 DIR 下所有 .txt → 按段落切块 → BGE 向量化 → 存 ChromaDB（{KB_DIR}）

说明:
    支持多个文件混入（每个文件独立切块，metadata 记录来源文件名）。
    ChromaDB 只负责「向量语义检索」，关键词检索（BM25）在检索阶段用 rank_bm25 另做。
"""

import argparse
import logging
import os
import re
import sys

# 保证 `python scripts/build_knowledge_base.py` 直接跑也能 import backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chromadb

from backend.core.config import KB_COLLECTION, KB_DIR, KB_SOURCE_DIR
from backend.core.embedding_client import BgeEmbeddingFunction

logger = logging.getLogger(__name__)

# 单个 chunk 的最大字符数（超过则按句末标点二次切分）
MAX_CHUNK_CHARS = 500


def read_text(path: str) -> str:
    """读文本文件，自动兼容 utf-8 / gbk 编码（中文 txt 常见两种编码混用）。"""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码: {path}")


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """长段落按句末标点（。！？；）切成不超过 max_chars 的块。"""
    sentences = re.split(r"(?<=[。！？；])", paragraph)
    chunks, buf = [], ""
    for s in sentences:
        if buf and len(buf) + len(s) > max_chars:
            chunks.append(buf.strip())
            buf = s
        else:
            buf += s
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """把整篇文本切成块：先按空行分段落，长段落再按句末标点二次切。"""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
        else:
            chunks.extend(_split_long(para, max_chars))
    return chunks


def load_chunks(source_dir: str) -> list[dict]:
    """扫描目录下所有 .txt，逐个切块，返回 [{text, source, chunk}]。"""
    if not os.path.isdir(source_dir):
        raise ValueError(f"源目录不存在: {source_dir}")

    files = sorted(f for f in os.listdir(source_dir) if f.lower().endswith(".txt"))
    if not files:
        raise ValueError(f"目录里没有 .txt 文件: {source_dir}")

    chunks = []
    for filename in files:
        text = read_text(os.path.join(source_dir, filename))
        file_chunks = split_text(text)
        for i, chunk in enumerate(file_chunks, start=1):
            chunks.append({"text": chunk, "source": filename, "chunk": i})
        logger.info("文件 %s 切出 %d 块", filename, len(file_chunks))
    return chunks


def build(source_dir: str = KB_SOURCE_DIR, persist_dir: str = KB_DIR, collection_name: str = KB_COLLECTION) -> None:
    """构建知识库：扫描目录 → 切块 → 向量化 → 存 ChromaDB。"""
    chunks = load_chunks(source_dir)
    if not chunks:
        raise ValueError(f"没有可用的文本块: {source_dir}")

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

    ids = [f"{c['source']}#{c['chunk']:03d}" for c in chunks]
    documents = [c["text"] for c in chunks]
    metadatas = [{"source": c["source"], "chunk": c["chunk"]} for c in chunks]

    batch = 32
    for start in range(0, len(chunks), batch):
        end = min(start + batch, len(chunks))
        collection.add(
            ids=ids[start:end],
            documents=documents[start:end],
            metadatas=metadatas[start:end],
        )
        logger.info("已写入 %d/%d 块", end, len(chunks))

    file_count = len({c["source"] for c in chunks})
    print(f"\n✅ 知识库构建完成: {len(chunks)} 块（来自 {file_count} 个文件）→ collection '{collection_name}' @ {persist_dir}")

    # 自测：查一条，验证端到端能检索
    test_q = "企业应当如何开展安全生产教育和培训？"
    res = collection.query(query_texts=[test_q], n_results=3)
    print(f"\n自测查询: {test_q}")
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        print(f"  [{meta['source']}#{meta['chunk']}] (距离={dist:.4f}) {doc[:40]}...")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

    parser = argparse.ArgumentParser(description="构建 RAG 知识库（扫描目录下 txt → ChromaDB）")
    parser.add_argument("--source-dir", default=KB_SOURCE_DIR, help="源目录，扫描其中所有 .txt")
    args = parser.parse_args()

    build(args.source_dir)
