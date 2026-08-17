"""知识库 ChromaDB 存储层 — 派生索引的读写。

权威源是 SQLite kb_files 登记表 + 源目录文件；
ChromaDB 只是检索引擎（派生数据），本模块是唯一直接操作它的入口。
"""

import logging

import chromadb

from backend.core.config import KB_COLLECTION, KB_DIR
from backend.core.embedding_client import BgeEmbeddingFunction

logger = logging.getLogger(__name__)

# 单个文件一次写入多少块（控制 embedding 批大小）
_BATCH = 32


def get_collection():
    """获取知识库 collection（不存在则创建）。"""
    client = chromadb.PersistentClient(path=KB_DIR)
    return client.get_or_create_collection(
        name=KB_COLLECTION,
        embedding_function=BgeEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"},
    )


def upsert_file_chunks(filename: str, chunks: list[str], md5: str) -> int:
    """把文件切好的块写入索引（先清掉该文件旧块再写），返回块数。"""
    collection = get_collection()
    try:
        collection.delete(where={"source": filename})
    except Exception:
        logger.debug("删除 %s 旧块时无匹配，忽略", filename)

    ids = [f"{filename}#{i:03d}" for i in range(1, len(chunks) + 1)]
    metadatas = [{"source": filename, "chunk": i, "md5": md5} for i in range(1, len(chunks) + 1)]
    for start in range(0, len(chunks), _BATCH):
        end = min(start + _BATCH, len(chunks))
        collection.upsert(
            ids=ids[start:end],
            documents=chunks[start:end],
            metadatas=metadatas[start:end],
        )
    return len(chunks)


def delete_file_chunks(filename: str) -> None:
    """从索引删除某个文件的全部块。"""
    collection = get_collection()
    try:
        collection.delete(where={"source": filename})
    except Exception:
        logger.debug("删除 %s 块时无匹配，忽略", filename)
