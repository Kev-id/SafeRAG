"""SafeRAG Backend — FastAPI 入口。

启动: uvicorn backend.main:app --host 0.0.0.0 --port 8080
"""

import asyncio
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

logger = logging.getLogger(__name__)

app = FastAPI(title="SafeRAG API", version="0.1.0")

# 启动时初始化数据库 + 拉起文档处理 worker
from backend.core.database import init_db
from backend.repositories import document_repo
from backend.services import document_service

@app.on_event("startup")
async def on_startup():
    init_db()
    # 启动恢复：上次进程退出时卡在 processing 的任务捞回 queued，worker 会接着跑
    recovered = document_repo.recover_stuck()
    if recovered:
        logger.info("已恢复 %d 个卡在 processing 的任务", recovered)
    # 拉起常驻任务消费 worker（存引用防止被 GC）
    app.state.worker_task = asyncio.create_task(document_service.worker())

# 挂载路由
from backend.api.ai import router as ai_router
from backend.api.documents import router as doc_router
from backend.api.tasks import router as tasks_router
from backend.api.kb import router as kb_router
from backend.api.health import router as health_router
from backend.api.chat import router as chat_router

app.include_router(ai_router)
app.include_router(doc_router)
app.include_router(tasks_router)
app.include_router(kb_router)
app.include_router(health_router)
app.include_router(chat_router)

@app.get("/")
async def root():
    return {"service": "SafeRAG API", "version": "0.1.0"}
