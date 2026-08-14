"""知识库构建脚本 — 扫描目录下所有 txt，增量同步到 ChromaDB。

用法:
    python scripts/build_knowledge_base.py [--source-dir DIR]

流程:
    扫描 DIR 下所有 .txt → 自适应切块 → 与库内已有块比对 → 只更新有变动的文件

说明:
    用文件内容 md5 判断变化：新增/修改的文件重新切块覆盖，删除的文件从库移除，未变化的跳过。
    支持多个文件混入（每个文件独立切块，metadata 记录来源文件名与 md5）。
    ChromaDB 只负责「向量语义检索」，关键词检索（BM25）在检索阶段用 rank_bm25 另做。
"""

import argparse
import hashlib
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
MAX_CHUNK_CHARS = 200


def read_text(path: str) -> str:
    """读文本文件，自动兼容 utf-8 / gbk 编码（中文 txt 常见两种编码混用）。"""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码: {path}")


def _file_md5(text: str) -> str:
    """基于归一化后的文本内容算 md5，用于判断文件是否变化。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


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


def _looks_like_line_items(lines: list[str]) -> bool:
    """判断是否"一行一条"格式：绝大多数行以句末标点结尾（每行是自包含完整句）。"""
    if not lines:
        return False
    ended = sum(1 for l in lines if l.endswith(("。", "！", "？", "；")))
    return ended / len(lines) >= 0.8


def _split_lines(lines: list[str], max_chars: int) -> list[str]:
    """按行切：每行一块，超长行按句末标点二次切。"""
    chunks = []
    for line in lines:
        if len(line) <= max_chars:
            chunks.append(line)
        else:
            chunks.extend(_split_long(line, max_chars))
    return chunks


def _split_paragraphs(text: str, max_chars: int) -> list[str]:
    """按空行分段落，长段按句末标点二次切。"""
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


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """自适应切块：
      - 一行一条（绝大多数行以句末标点结尾）→ 按行切，适合法规条文
      - 成段文章 → 按空行分段落切
    长块统一按句末标点二次切到 max_chars 以内。
    """
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l]

    if _looks_like_line_items(lines):
        return _split_lines(lines, max_chars)
    return _split_paragraphs(text, max_chars)


def build(source_dir: str = KB_SOURCE_DIR, persist_dir: str = KB_DIR, collection_name: str = KB_COLLECTION, force: bool = False) -> None:
    """增量同步知识库：只重切 + 覆盖有变动的文件，未变化的跳过。

    force=True 时先清空 collection 再全量重建（切块策略调整后需要整体重切时用）。
    """
    if not os.path.isdir(source_dir):
        raise ValueError(f"源目录不存在: {source_dir}")

    files = sorted(f for f in os.listdir(source_dir) if f.lower().endswith(".txt"))
    if not files:
        raise ValueError(f"目录里没有 .txt 文件: {source_dir}")

    # 1. 读源目录：{filename: (text, md5)}
    source_map: dict[str, tuple[str, str]] = {}
    for filename in files:
        text = read_text(os.path.join(source_dir, filename))
        source_map[filename] = (text, _file_md5(text))

    client = chromadb.PersistentClient(path=persist_dir)

    if force:
        try:
            client.delete_collection(collection_name)
            logger.info("--force：已删除旧 collection '%s'，将全量重建", collection_name)
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=BgeEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"},
    )

    # 2. 读库里已有块：{filename: md5}（只取 metadata，不加载全部文本）
    existing_metas = collection.get(include=["metadatas"])["metadatas"]
    existing_md5 = {m["source"]: m.get("md5", "") for m in existing_metas if "source" in m}

    # 3. 删除：库里还有、源目录里已移除的文件
    for filename in existing_md5:
        if filename not in source_map:
            collection.delete(where={"source": filename})
            logger.info("删除已移除文件 %s 的旧块", filename)

    # 4. 新增 / 修改：md5 变了才重切覆盖；未变化跳过
    batch = 32
    updated, skipped = 0, 0
    for filename, (text, md5) in source_map.items():
        if existing_md5.get(filename) == md5:
            skipped += 1
            logger.info("文件 %s 未变化，跳过", filename)
            continue

        if filename in existing_md5:
            collection.delete(where={"source": filename})

        file_chunks = split_text(text)
        ids = [f"{filename}#{i:03d}" for i in range(1, len(file_chunks) + 1)]
        metadatas = [{"source": filename, "chunk": i, "md5": md5} for i in range(1, len(file_chunks) + 1)]
        for start in range(0, len(file_chunks), batch):
            end = min(start + batch, len(file_chunks))
            collection.upsert(
                ids=ids[start:end],
                documents=file_chunks[start:end],
                metadatas=metadatas[start:end],
            )
        updated += 1
        logger.info("已更新文件 %s（%d 块）", filename, len(file_chunks))

    # 汇总
    final_metas = collection.get(include=["metadatas"])["metadatas"]
    file_count = len({m["source"] for m in final_metas})
    print(f"\n✅ 知识库同步完成: 共 {len(final_metas)} 块 / {file_count} 个文件（更新 {updated}，跳过 {skipped}）→ '{collection_name}' @ {persist_dir}")

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
    parser.add_argument("--force", action="store_true", help="清空 collection 全量重建（切块策略调整后用）")
    args = parser.parse_args()

    build(args.source_dir, force=args.force)
