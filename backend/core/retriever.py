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
import threading

# 保证 `python backend/core/retriever.py` 直接跑也能 import backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import chromadb
import jieba
from rank_bm25 import BM25Okapi

from backend.core.config import KB_COLLECTION, KB_DIR
from backend.core.embedding_client import BgeEmbeddingFunction, tokenize_with_offsets
from backend.core.kb_store import get_all_batch

logger = logging.getLogger(__name__)

# RRF 常数：决定 rank 的衰减速度，60 是业界常用值
RRF_K = 60

# BGE 位置编码上限：query 超此 token 数必须分段，否则 embedding 失效
MAX_QUERY_TOKENS = 512
# 每段 token 上限：留余量，避免切分后重编码漂移超出 512
SEG_TOKENS = 480

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
        # 只保护"加载/构建 BM25"这一段；检索路径（_bm25 已就绪）不进锁
        self._load_lock = threading.Lock()
        # 加载是否进行中（供 health 等展示"检索器加载中"）
        self._loading = False

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        """第一次检索时加载 ChromaDB + 构建 BM25 索引（幂等，线程安全）。

        double-checked locking：外面先查快路径，锁内再查确认——
        多线程（后台预热 + RAG 检索并发）同时进入时，只有真正抢到锁的
        线程执行构建，其余等待者在锁内看到 _bm25 已就绪直接返回。
        """
        if self._bm25 is not None:
            return
        with self._load_lock:
            if self._bm25 is not None:  # 可能别的线程已构建完
                return
            self._do_load()

    def _do_load(self) -> None:
        """实际加载（持锁调用，仅一次）。"""
        self._loading = True
        try:
            client = chromadb.PersistentClient(path=self._persist_dir)
            self._collection = client.get_collection(
                name=self._collection_name,
                embedding_function=BgeEmbeddingFunction(),
            )

            data = get_all_batch(self._collection, include=["documents", "metadatas"])
            self._ids = data["ids"]
            self._docs = data["documents"]
            self._metas = data["metadatas"]

            # BM25 索引：对每个文档分词
            tokenized = [jieba.lcut(doc) for doc in self._docs]
            self._bm25 = BM25Okapi(tokenized)
            logger.info("检索器已加载: %d 条文档", len(self._docs))
        finally:
            self._loading = False

    def is_loading(self) -> bool:
        """检索器是否正在加载（后台预热期间为 True，供 health 展示）。"""
        return self._loading

    # ------------------------------------------------------------------
    # 两个子检索器
    # ------------------------------------------------------------------
    def _candidate_ids(self, provinces: list[str] | None = None,
                       cities: list[str] | None = None,
                       file_types: list[str] | None = None) -> set[str] | None:
        """三层筛选缩候选集：先 file_type，地方法规再省市。

        语义（用户拍板 A —— file_type 是最外层，勾地方法规只看地方法规）：
          - 什么都不勾 → None（全量）
          - 只勾 file_type（如["国家法律"]）→ 仅该类型，不进省市
          - 勾["地方法规"]（+ 可选省市）→ 只地方法规，走省市层过滤
          - 不勾 file_type 但勾省市（旧语义兼容）→ 国家法律始终保留 + 省/市地方法规
        国家法律仅在"未做 file_type 筛选"或"file_type 含国家法律"时保留。
        """
        has_ft = bool(file_types)
        has_region = bool(provinces or cities)
        if not has_ft and not has_region:
            return None
        ftset = set(file_types) if has_ft else None
        pset = set(provinces or [])
        cset = set(cities or [])
        cand: set[str] = set()
        for i, m in enumerate(self._metas):
            ft = m.get("file_type", "") or ""
            if ftset is not None and ft not in ftset:
                continue  # file_type 筛选未命中
            if ft == "国家法律":
                # 国家法仅当没做类型筛选（旧语义）或类型筛选含国家法时保留
                if ftset is None or "国家法律" in ftset:
                    cand.add(self._ids[i])
                continue
            # 非国家法（地方法规/其它）：
            if not has_region:
                cand.add(self._ids[i])  # 只有 file_type 筛选 → 该类型全保留
                continue
            prov = m.get("region", "") or ""
            city = m.get("city", "") or ""
            # 省级条例（city 空）：仅当勾了省且该省在选中
            if not city:
                if pset and prov in pset:
                    cand.add(self._ids[i])
                continue
            # 市级条例：province 匹配（prov 空则不限）+ city 在选中
            if cset and city in cset and (not pset or prov in pset):
                cand.add(self._ids[i])
        return cand

    def _bm25_ids(self, query: str, top_n: int, candidate_ids: set[str] | None = None) -> list[str]:
        """BM25 关键词检索，返回按相关度排序的 doc id。

        candidate_ids 非空时，只在这些候选（国家法律+事发地条例）里打分排序，
        其它省/市的地方性法规不参与，从根上避免"上海事故查到湖北条例"。
        """
        scores = self._bm25.get_scores(jieba.lcut(query))
        if candidate_ids is not None:
            cand_idx = [i for i, did in enumerate(self._ids) if did in candidate_ids]
            ranked = sorted(cand_idx, key=lambda i: -scores[i])[:top_n]
        else:
            ranked = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_n]
        return [self._ids[i] for i in ranked]

    def _split_query(self, query: str) -> list[str]:
        """把超长 query 按 token 边界切成 ≤512 的段；短 query 原样返回。"""
        spans = tokenize_with_offsets(query)
        if len(spans) <= MAX_QUERY_TOKENS:
            return [query]
        segments = []
        for i in range(0, len(spans), SEG_TOKENS):
            chunk = spans[i:i + SEG_TOKENS]
            segments.append(query[chunk[0][0]:chunk[-1][1]])
        logger.info("query 超长，分段检索: %d 段", len(segments))
        return segments

    def _vector_ids(self, query: str, top_n: int, candidate_ids: set[str] | None = None) -> list[str]:
        """向量语义检索：超长 query 按 token 分段查询后合并去重。

        candidate_ids 非空时，只保留候选集内的命中（国家法律+事发地条例）；
        其它省/市的地方性法规即使向量相近也不纳入。取数放宽到 top_n*3，
        避免候选内真实相关项因全库排序靠后被截掉。
        """
        all_ids: list[str] = []
        seen: set[str] = set()
        fetch_n = top_n * 3 if candidate_ids is not None else top_n
        for seg in self._split_query(query):
            res = self._collection.query(query_texts=[seg], n_results=fetch_n)
            for doc_id in res["ids"][0]:
                if candidate_ids is not None and doc_id not in candidate_ids:
                    continue
                if doc_id not in seen:
                    seen.add(doc_id)
                    all_ids.append(doc_id)
            if len(all_ids) >= top_n:
                break
        return all_ids[:top_n]

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
    def warmup(self) -> None:
        """预热：加载 ChromaDB 全量 + 构建 BM25 索引（幂等）。

        供服务启动时后台调用，让第一次检索不必等待几秒到几十秒的冷启动。
        """
        self._ensure_loaded()

    def retrieve(self, query: str, top_k: int = 5,
                 provinces: list[str] | None = None,
                 cities: list[str] | None = None,
                 file_types: list[str] | None = None) -> list[dict]:
        """混合检索，返回 top_k 条，每条含 id/text/meta/score。

        三层筛选（打分前候选化）：
          file_types →（地方法规）→ provinces → cities
        file_types 为 None 时保持旧语义（国家法始终 + 省市地方法规）。
        全部空 → 全量检索。见 _candidate_ids 语义。
        """
        self._ensure_loaded()

        candidate_ids = self._candidate_ids(provinces, cities, file_types) \
            if (provinces or cities or file_types) else None
        bm25_ids = self._bm25_ids(query, top_n=top_k * 2, candidate_ids=candidate_ids)
        vec_ids = self._vector_ids(query, top_n=top_k * 2, candidate_ids=candidate_ids)
        merged = self._rrf([bm25_ids, vec_ids], top_k=top_k)

        id2idx = {doc_id: i for i, doc_id in enumerate(self._ids)}

        def hit(doc_id, score):
            idx = id2idx[doc_id]
            return {
                "id": doc_id,
                "text": self._docs[idx],
                "meta": self._metas[idx],
                "score": round(score, 5),
            }

        results = [hit(did, sc) for did, sc in merged]

        # 双保险：候选集内约束后，仍按候选集精确过滤一次（避免非匹配省/市混入）
        if candidate_ids is not None:
            results = [h for h in results if h["id"] in candidate_ids]
        return results


# 全局单例：后端启动后复用同一个检索器，避免每次请求重建 BM25 索引
_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    """获取全局唯一检索器（懒加载，第一次调用才初始化）。"""
    global _retriever
    if _retriever is None:
        _retriever = Retriever()
    return _retriever

def reset_retriever() -> None:
    """知识库重建后调用，让下次 get_retriever() 重新加载。"""
    global _retriever
    _retriever = None


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
            src = h["meta"].get("source", "?")
            chunk = h["meta"].get("chunk", "?")
            print(f"  [{src}#{chunk}] (RRF={h['score']:.4f}) {h['text'][:40]}...")