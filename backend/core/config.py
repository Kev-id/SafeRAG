"""应用配置，集中管理所有可调参数。"""

import os

# ---- Qwen 推理引擎 ----
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "http://127.0.0.1:8000")
QWEN_MODEL = os.getenv("QWEN_MODEL", "tpu-qwen3.5")
QWEN_TIMEOUT = int(os.getenv("QWEN_TIMEOUT", "300"))

# ---- Embedding 模型（RAG 检索用）----
EMBEDDING_MODEL_PATH = os.getenv("EMBEDDING_MODEL_PATH", "/data2/models/bge-small-zh-v1.5")

# ---- 数据存储 ----
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(DATA_DIR, 'saferag.db')}",
)

# ---- 服务 ----
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8081"))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
