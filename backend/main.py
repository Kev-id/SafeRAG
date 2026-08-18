"""SafeRAG Backend — FastAPI 入口。

启动: uvicorn backend.main:app --host 0.0.0.0 --port 8080
"""

import logging
import sys
import os

# 确保 backend.xxx 导入正确
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

app = FastAPI(title="SafeRAG API", version="0.1.0")

# 启动时初始化数据库
from backend.core.database import init_db

@app.on_event("startup")
async def on_startup():
    init_db()

# 挂载路由
from backend.api.ai import router as ai_router
from backend.api.documents import router as doc_router
from backend.api.tasks import router as tasks_router
from backend.api.kb import router as kb_router
from backend.api.health import router as health_router

app.include_router(ai_router)
app.include_router(doc_router)
app.include_router(tasks_router)
app.include_router(kb_router)
app.include_router(health_router)

@app.get("/")
async def root():
    return {"service": "SafeRAG API", "version": "0.1.0"}
