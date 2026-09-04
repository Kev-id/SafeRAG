"""Reranker 客户端 — 可选精排层（bge-reranker cross-encoder）。

BM25+向量+RRF 是"检索"，不精确；reranker 是"精排"：把粗取出的候选池
逐条和 query 做 cross-encoder（序列对）打分，得到比 BM25/向量点积更准的
相关性排序。模型、结构与 embedding 相同（ONNX + tokenizers 纯 Rust）：
    {RERANKER_MODEL_PATH}/
        onnx/model_quantized.onnx
        tokenizer.json

**完全可选，降级安全**：
  - RERANKER_MODEL_PATH 留空 / 模型文件缺失 → is_available()=False，
    retriever 不接入精排，检索完全走原来的 BM25+向量+RRF。
  - 运行时加载或推理抛错 → rerank() 记日志后把 hits 原顺序返回，
    不影响结果产出（仍是粗排池里的 top_k，只是没精排）。
"""

import logging
import os
import sys

# 保证 `python backend/core/reranker.py` 直接跑也能 import backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from backend.core.config import RERANKER_MODEL_PATH

logger = logging.getLogger(__name__)

# cross-encoder 位置编码上限 512，留 2 位余量（同 embedding）
_MAX_SEQ_TOKENS = 510

# 懒加载缓存：模型第一次用时才加载，之后驻留内存
_session: ort.InferenceSession | None = None
_tokenizer: Tokenizer | None = None
_input_names: list[str] = []


def is_available() -> bool:
    """模型文件是否就位（留空此模型 = 不启用精排）。

    只探文件存在性（便宜 stat），不触发加载——真正加载在 rerank() 首次调用时。
    """
    if not RERANKER_MODEL_PATH:
        return False
    return os.path.isfile(_model_path()) and os.path.isfile(_tokenizer_path())


def _model_path() -> str:
    return os.path.join(RERANKER_MODEL_PATH, "onnx", "model_quantized.onnx")


def _tokenizer_path() -> str:
    return os.path.join(RERANKER_MODEL_PATH, "tokenizer.json")


def _ensure_loaded() -> None:
    """第一次调用时加载模型（幂等，之后直接复用）。"""
    global _session, _tokenizer, _input_names
    if _session is not None:
        return
    if not os.path.isfile(_model_path()):
        raise FileNotFoundError(f"未找到 reranker 模型: {_model_path()}")
    _tokenizer = Tokenizer.from_file(_tokenizer_path())
    # cross-encoder 单次打分就是 query+passage 一对，总长上限 512；
    # 开启截断（保留开头），避免超长 chunk 触发 ONNX 广播报错。
    _tokenizer.enable_truncation(max_length=_MAX_SEQ_TOKENS)
    _session = ort.InferenceSession(_model_path(), providers=["CPUExecutionProvider"])
    _input_names = [i.name for i in _session.get_inputs()]
    logger.info("reranker 模型已加载: %s (输入节点: %s)", _model_path(), _input_names)


def _score_batch(query: str, texts: list[str]) -> np.ndarray:
    """把 query 与每条 passage 组成 token 对，批量跑 cross-encoder。

    返回 shape=(n,) 的 logit 分数（越大越相关）。
    """
    encodings = [_tokenizer.encode(query, t) for t in texts]

    def _type_ids(e):
        tids = getattr(e, "type_ids", None)
        if callable(tids):
            tids = tids()
        if tids is None:  # 老版本 tokenizers 不返回 type_ids，退化为全 0
            tids = [0] * len(e.ids)
        return tids

    lengths = [min(_MAX_SEQ_TOKENS, len(e.ids)) for e in encodings]
    max_len = max(lengths)

    input_ids, attention_mask, token_type_ids = [], [], []
    for e, L in zip(encodings, lengths):
        ids = e.ids[:L]
        mask = e.attention_mask[:L]
        tids = _type_ids(e)[:L]
        pad = max_len - L
        input_ids.append(ids + [0] * pad)
        attention_mask.append(mask + [0] * pad)
        token_type_ids.append(tids + [0] * pad)

    feed = {}
    for name in _input_names:
        if name == "input_ids":
            feed[name] = np.array(input_ids, dtype=np.int64)
        elif name == "attention_mask":
            feed[name] = np.array(attention_mask, dtype=np.int64)
        elif "token_type" in name:
            feed[name] = np.array(token_type_ids, dtype=np.int64)

    outputs = _session.run(None, feed)
    scores = np.asarray(outputs[0])
    # 分类头输出通常是 (batch, 1) 或 (batch,)，展平成 (batch,)
    return scores.reshape(len(texts)).astype(float)


def rerank(query: str, hits: list[dict]) -> list[dict]:
    """按 query 对 hits 精排，返回重排后的 hits（高分在前）。

    hits: retriever.retrieve 的输出（含 id/text/meta/score）。
    精排后 score 换成 reranker logit（越相关越大）。
    任何加载/推理异常 → 记日志、原顺序返回，绝不让精排拖垮检索主链路。
    """
    if not hits:
        return hits
    try:
        _ensure_loaded()
    except Exception as e:
        logger.warning("reranker 不可用，跳过精排（原顺序返回）: %s", e)
        return hits
    try:
        scores = _score_batch(query, [h["text"] for h in hits])
        ranked = sorted(zip(hits, scores), key=lambda hs: -hs[1])
        return [{**h, "score": round(float(s), 5)} for h, s in ranked]
    except Exception as e:
        logger.warning("reranker 推理失败，跳过精排（原顺序返回）: %s", e)
        return hits


if __name__ == "__main__":
    # 自测：模型存在时验证精排能区分相关/无关
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    print("reranker 路径:", RERANKER_MODEL_PATH, "| 可用:", is_available())
    if not is_available():
        print("（未配置模型，跳过打分自测）")
        raise SystemExit(0)

    hits = [
        {"id": "a", "text": "高处作业人员必须按规定佩戴安全带，防止坠落事故。",
         "meta": {}, "score": 0.1},
        {"id": "b", "text": "食堂食材采购应当索证索票，确保食品安全。",
         "meta": {}, "score": 0.2},
    ]
    out = rerank("高处作业需要系安全带吗", hits)
    for h in out:
        print(f"  [{h['id']}] score={h['score']:.4f} {h['text'][:20]}...")
    assert out[0]["id"] == "a", "相关的（高处作业）应排前面"
    print("\n✅ reranker 自测通过")