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

# 懒加载缓存：client / collection 第一次用时才创建，之后复用。
# 避免每次上传/删除都重连 ChromaDB（重连会反复加载 embedding function，批量上传时明显变慢）。
_client = None
_collection = None


def get_collection():
    """获取知识库 collection（懒加载，进程内复用同一个实例）。"""
    global _client, _collection#global的声明是为了在函数内部修改全局变量的值，而不是创建一个新的局部变量。
    if _collection is None:
        _client = chromadb.PersistentClient(path=KB_DIR)
        _collection = _client.get_or_create_collection(
            name=KB_COLLECTION,
            embedding_function=BgeEmbeddingFunction(),
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def upsert_file_chunks(
    filename: str,
    chunks: list[str],
    md5: str,
    metadatas: list[dict] | None = None,
    embeddings: list[list[float]] | None = None,
) -> int:
    """把文件切好的块写入索引（先清掉该文件旧块再写），返回块数。

    embeddings 非空时只把向量直插（跳过 embedding 函数）——供 build 预计算好
    向量后写库，避免 Chroma 内部再嵌一遍；None 则让 Chroma 用 embedding 函数现算。
    """
    collection = get_collection()
    try:
        collection.delete(where={"source": filename})
    except Exception:
        logger.debug("删除 %s 旧块时无匹配，忽略", filename)

    ids = [f"{filename}#{i:03d}" for i in range(1, len(chunks) + 1)]
    if metadatas is None:
        metadatas = [{"source": filename, "chunk": i, "md5": md5} for i in range(1, len(chunks) + 1)]
    else:
        metadatas = [
            {
                **meta,
                "source": filename,
                "chunk": i,
                "md5": md5,
            }
            for i, meta in enumerate(metadatas, start=1)
        ]
    for start in range(0, len(chunks), _BATCH):
        end = min(start + _BATCH, len(chunks))
        kw = dict(
            ids=ids[start:end],
            documents=chunks[start:end],
            metadatas=metadatas[start:end],
        )
        if embeddings is not None:
            kw["embeddings"] = embeddings[start:end]
        collection.upsert(**kw)
    return len(chunks)


def delete_file_chunks(filename: str) -> None:
    """从索引删除某个文件的全部块。"""
    collection = get_collection()
    try:
        collection.delete(where={"source": filename})
    except Exception:
        logger.debug("删除 %s 块时无匹配，忽略", filename)


def check_chroma_health() -> bool:
    """检查 ChromaDB 是否可用（collection 可访问）。"""
    try:
        collection = get_collection()
        _ = collection.count()
        return True
    except Exception as e:
        logger.error("ChromaDB 健康检查失败: %s", e)
        return False


# 分批拉取条数：低于 SQLite 单查询变量上限（默认 999），
# 避免全量 get() 把所有 id 拼进一条 IN 查询触发 "too many SQL variables"
_GET_BATCH = 500


def get_all_batch(collection, include: list[str] | None = None) -> dict:
    """分批拉取 collection 全量数据（ids/documents/metadatas/embeddings）。

    大知识库（千级文件、数万 chunk）全量调用 collection.get() 会触发
    SQLite "too many SQL variables"（全量 id 拼进一条 IN 查询超限）。
    用 limit/offset 分页逐批拉，规避该限制。

    返回 dict: {"ids": [...], **include 中的键}。include 为 None 时按
    chromadb 默认返回（documents + metadatas）。
    """
    if include is None:
        include = ["documents", "metadatas"]
    result: dict = {"ids": []}
    for k in include:
        result[k] = []

    offset = 0
    while True:
        batch = collection.get(include=include, limit=_GET_BATCH, offset=offset)
        ids = batch.get("ids") or []
        if not ids:
            break
        result["ids"].extend(ids)
        for k in include:
            result[k].extend(batch.get(k) or [])
        offset += len(ids)
        # 防止极端情况下 offset 不前进的死循环
        if len(ids) < _GET_BATCH:
            break
    return result
