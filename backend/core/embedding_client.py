"""Embedding 客户端 — 进程内加载 BGE 模型做文本向量化。

与 qwen_client（走 HTTP 端口 8000）不同，embedding 模型很小，
直接加载进当前进程内存，作为普通函数调用：

    - onnxruntime 跑 ONNX 模型（int8 量化，约 24MB）
    - tokenizers 做分词（纯 Rust，不依赖 torch）
    - BGE 官方用 CLS pooling（取 [CLS] token 输出）+ L2 归一化

模型目录结构（Xenova 导出的仓库）：
    {EMBEDDING_MODEL_PATH}/
        onnx/model_quantized.onnx
        tokenizer.json
"""

import os
import sys
import logging

# 保证 `python backend/core/embedding_client.py` 直接跑也能 import backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from backend.core.config import EMBEDDING_MODEL_PATH

logger = logging.getLogger(__name__)

# 懒加载缓存：模型第一次用时才加载，之后驻留内存
_session: ort.InferenceSession | None = None
_tokenizer: Tokenizer | None = None
_input_names: list[str] = []


def _ensure_loaded() -> None:
    """第一次调用时加载模型（幂等，之后直接复用）。"""
    global _session, _tokenizer, _input_names
    if _session is not None:
        return

    model_path = os.path.join(EMBEDDING_MODEL_PATH, "onnx", "model_quantized.onnx")
    tokenizer_path = os.path.join(EMBEDDING_MODEL_PATH, "tokenizer.json")

    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"未找到 embedding 模型: {model_path}")

    _tokenizer = Tokenizer.from_file(tokenizer_path)
    _session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    _input_names = [i.name for i in _session.get_inputs()]
    logger.info("embedding 模型已加载: %s (输入节点: %s)", model_path, _input_names)


def _encode_batch(texts: list[str]) -> tuple[list[list[int]], list[list[int]], list[list[int]]]:
    """批量编码，并 padding 到同一长度。

    返回 (input_ids, attention_mask, token_type_ids)，三个都是等长二维列表。
    """
    encodings = [_tokenizer.encode(t) for t in texts]
    max_len = max(len(e.ids) for e in encodings)

    input_ids, attention_mask, token_type_ids = [], [], []
    for e in encodings:
        pad = max_len - len(e.ids)
        input_ids.append(e.ids + [0] * pad)
        attention_mask.append(e.attention_mask + [0] * pad)
        # BERT 单句输入，token_type 全 0
        tids = getattr(e, "type_ids", [0] * len(e.ids))
        token_type_ids.append(tids + [0] * pad)
    return input_ids, attention_mask, token_type_ids


def embed(texts: str | list[str]) -> np.ndarray:
    """把文本向量化，返回 shape=(n, dim) 的 L2 归一化向量。

    归一化后，向量点积 == 余弦相似度，可直接用于检索。
    """
    if isinstance(texts, str):
        texts = [texts]
    _ensure_loaded()

    input_ids, attention_mask, token_type_ids = _encode_batch(texts)

    # 按模型实际输入节点名构造 feed dict（不同导出节点名可能不同）
    feed = {}
    for name in _input_names:
        if name == "input_ids":
            feed[name] = np.array(input_ids, dtype=np.int64)
        elif name == "attention_mask":
            feed[name] = np.array(attention_mask, dtype=np.int64)
        elif "token_type" in name:
            feed[name] = np.array(token_type_ids, dtype=np.int64)

    outputs = _session.run(None, feed)
    vec = outputs[0]  # 第一个输出，通常是 last_hidden_state

    # BGE 用 CLS pooling：取 [CLS] token（第 0 个位置）
    if vec.ndim == 3:
        vec = vec[:, 0, :]

    # L2 归一化
    norms = np.linalg.norm(vec, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return vec / norms


if __name__ == "__main__":
    # 自测：验证维度、归一化、语义正确性
    logging.basicConfig(level=logging.INFO)

    print("模型路径:", EMBEDDING_MODEL_PATH)

    v = embed("安全生产事故隐患排查治理")
    print("向量维度:", v.shape)
    print("前 5 个值:", v[0, :5])
    print("模长:", float(np.linalg.norm(v[0])), "(应≈1)")

    # 语义验证：相关文本相似度应高于无关文本
    a = embed("高处作业未系安全带导致坠落")[0]
    b = embed("工人从脚手架摔落受伤")[0]
    c = embed("食堂今天供应红烧肉")[0]
    sim_ab = float(a @ b)
    sim_ac = float(a @ c)
    print(f"相关文本相似度: {sim_ab:.4f}")
    print(f"无关文本相似度: {sim_ac:.4f}")
    assert sim_ab > sim_ac, "语义相近的文本相似度应更高，检查 pooling 方式是否正确"

    print("\n✅ embedding 自测通过")
