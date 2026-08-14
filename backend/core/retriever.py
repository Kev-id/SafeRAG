"""检索器 — 混合检索（BM25 关键词 + 向量语义），RRF 融合排名。

法律条文检索的两种互补方式：
  - BM25：擅长精确术语匹配（"特种作业""隐患排查""第X条"）
  - 向量：擅长语义相近（"高空坠落" vs "高处作业"）
  两者各自返回一个排名，用 RRF（Reciprocal Rank Fusion）融合。

用法:
    retriever = Retriever()
    hits = retriever.retrieve("企业如何开展安全培训", top_k=5)
    # hits: [{"id", "text", "meta", "score"}, ...]
"""

import logging
import os
import sys

# 保证 `python backend/core/retriever.py` 直接跑也能 import backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import chromadb
import jieba
from rank_bm25 import BM25Okapi

from backend.core.config import KB_COLLECTION, KB_DIR
from backend.core.embedding_client import BgeEmbeddingFunction

logger = logging.getLogger(__name__)

# RRF 常数：决定 rank 的衰减速度，60 是业界常用值
RRF_K = 60

# jieba 首次加载词典会往 stderr 打印进度，静音
jieba.setLogLevel(logging.WARNING)


class Retriever:
    """懒加载知识库（ChromaDB 向量 + BM25 索引），提供混合检索。"""

    def __init__(self, persist_dir: str = KB_DIR, collection_name: str = KB_COLLECTION):
        self._persist_dir = persist_dir
        self._collection_name = collection_name
        self._collection = None
        self._ids: list[str] = []
        self._docs: list[str] = []
        self._metas: list[dict] = []
        self._bm25: BM25Okapi | None = None

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        """第一次检索时加载 ChromaDB + 构建 BM25 索引（幂等）。"""
        if self._bm25 is not None:
            return

        client = chromadb.PersistentClient(path=self._persist_dir)
        self._collection = client.get_collection(
            name=self._collection_name,
            embedding_function=BgeEmbeddingFunction(),
        )

        data = self._collection.get()
        self._ids = data["ids"]
        self._docs = data["documents"]
        self._metas = data["metadatas"]

        # BM25 索引：对每个文档分词
        tokenized = [jieba.lcut(doc) for doc in self._docs]
        self._bm25 = BM25Okapi(tokenized)
        logger.info("检索器已加载: %d 条文档", len(self._docs))

    # ------------------------------------------------------------------
    # 两个子检索器
    # ------------------------------------------------------------------
    def _bm25_ids(self, query: str, top_n: int) -> list[str]:
        """BM25 关键词检索，返回按相关度排序的 doc id。"""
        scores = self._bm25.get_scores(jieba.lcut(query))
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_n]
        return [self._ids[i] for i in ranked]

    def _vector_ids(self, query: str, top_n: int) -> list[str]:
        """向量语义检索，返回按相似度排序的 doc id。"""
        res = self._collection.query(query_texts=[query], n_results=top_n)
        return res["ids"][0]

    # ------------------------------------------------------------------
    # RRF 融合
    # ------------------------------------------------------------------
    @staticmethod
    def _rrf(id_rankings: list[list[str]], top_k: int) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion：合并多个排名，返回 [(id, 融合分)]。"""
        scores: dict[str, float] = {}
        for ranking in id_rankings:
            for rank, doc_id in enumerate(ranking, start=1):
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
        return sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        """混合检索，返回 top_k 条，每条含 id/text/meta/score。"""
        self._ensure_loaded()

        bm25_ids = self._bm25_ids(query, top_n=top_k * 2)
        vec_ids = self._vector_ids(query, top_n=top_k * 2)
        merged = self._rrf([bm25_ids, vec_ids], top_k=top_k)

        id2idx = {doc_id: i for i, doc_id in enumerate(self._ids)}
        results = []
        for doc_id, score in merged:
            idx = id2idx[doc_id]
            results.append({
                "id": doc_id,
                "text": self._docs[idx],
                "meta": self._metas[idx],
                "score": round(score, 5),
            })
        return results


# 全局单例：后端启动后复用同一个检索器，避免每次请求重建 BM25 索引
_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    """获取全局唯一检索器（懒加载，第一次调用才初始化）。"""
    global _retriever
    if _retriever is None:
        _retriever = Retriever()
    return _retriever


if __name__ == "__main__":
    # 自测：几个典型问题，验证混合检索
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

    retriever = Retriever()
    queries = [
        "企业如何开展安全生产教育和培训？",
        "进行爆破、动火等危险作业有什么要求？",
        "事故隐患排查治理要怎么做？",
    ]
    for q in queries:
        hits = retriever.retrieve(q, top_k=3)
        print(f"\n查询: {q}")
        for h in hits:
            line = h["meta"].get("line", "?")
            print(f"  [第{line}行] (RRF={h['score']:.4f}) {h['text'][:40]}...")