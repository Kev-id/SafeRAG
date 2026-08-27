"""应用配置，集中管理所有可调参数。"""

import os

# ---- Qwen 推理引擎 ----
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "http://127.0.0.1:8000")
QWEN_MODEL = os.getenv("QWEN_MODEL", "tpu-qwen3.5")
# 按用途隔离：文档处理 / 流式对话各一个引擎，默认回退单引擎（QWEN_BASE_URL/QWEN_MODEL）。
# 文档以后切 4B：把 QWEN_DOC_URL 指向跑 4B 的端口 + QWEN_DOC_MODEL 设对应模型名即可，代码零改动。
QWEN_DOC_URL = os.getenv("QWEN_DOC_URL", QWEN_BASE_URL)
QWEN_CHAT_URL = os.getenv("QWEN_CHAT_URL", QWEN_BASE_URL)
QWEN_DOC_MODEL = os.getenv("QWEN_DOC_MODEL", QWEN_MODEL)
QWEN_CHAT_MODEL = os.getenv("QWEN_CHAT_MODEL", QWEN_MODEL)
# 连接超时：局域网内应 <1s，设 5s 防误报
QWEN_CONNECT_TIMEOUT = int(os.getenv("QWEN_CONNECT_TIMEOUT", "5"))
# 读超时：8K 上下文模型长推理可能几十秒到几分钟，设 600s
QWEN_READ_TIMEOUT = int(os.getenv("QWEN_READ_TIMEOUT", "600"))
# 单次生成的最大 token 数：封顶最坏耗时（配合推理引擎的 max_tokens）
QWEN_MAX_TOKENS = int(os.getenv("QWEN_MAX_TOKENS", "4096"))

# ---- Embedding 模型（RAG 检索用）----
EMBEDDING_MODEL_PATH = os.getenv("EMBEDDING_MODEL_PATH", "/data2/models/bge-small-zh-v1.5")

# ---- 数据存储 ----
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(DATA_DIR, 'saferag.db')}",
)

# ---- RAG 知识库（ChromaDB）----
KB_DIR = os.getenv("KB_DIR", os.path.join(DATA_DIR, "kb"))
KB_SOURCE_DIR = os.getenv("KB_SOURCE_DIR", os.path.join(DATA_DIR, "kb_source"))
KB_COLLECTION = os.getenv("KB_COLLECTION", "regulations")

# ---- 服务 ----
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8081"))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
