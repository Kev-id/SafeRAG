"""业务层 — 健康检查。"""

from backend.core.qwen_client import check_engines
from backend.core.database import check_database_health
from backend.core.kb_store import check_chroma_health


async def get_health_status() -> dict:
    engines = await check_engines()
    sqlite_health = check_database_health()
    chroma_health = check_chroma_health()

    # 引擎池下，任一引擎可达即认为 qwen 健康（响应字段保持不变，前端契约零变化）
    qwen_ok = any(e["reachable"] for e in engines)
    status = "ok" if sqlite_health and qwen_ok and chroma_health else "degraded"
    return {
        "status": status,
        "sqlite": sqlite_health,
        "qwen": qwen_ok,
        "chroma": chroma_health,
    }
