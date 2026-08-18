"""业务层 — 文档处理核心逻辑。


"""
from backend.core.qwen_client import check_health
from backend.core.database import check_database_health
from backend.core.kb_store import check_chroma_health

async def get_health_status():
    qwen_health = await check_health()
    sqlite_health = check_database_health()
    chroma_health = check_chroma_health()
    status = "ok" if sqlite_health and qwen_health and chroma_health else "degraded"
    return {
        "status": status,
        "sqlite": sqlite_health,
        "qwen": qwen_health,
        "chroma": chroma_health,
    }