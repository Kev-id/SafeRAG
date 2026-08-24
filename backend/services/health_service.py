"""业务层 — 健康检查。"""

from backend.core.qwen_client import check_health, check_chat_health
from backend.core.database import check_database_health
from backend.core.kb_store import check_chroma_health


async def get_health_status() -> dict:
    doc_health = await check_health()
    chat_health = await check_chat_health()
    sqlite_health = check_database_health()
    chroma_health = check_chroma_health()

    engines_ok = doc_health["reachable"] and chat_health["reachable"]
    status = "ok" if sqlite_health and engines_ok and chroma_health else "degraded"
    return {
        "status": status,
        "sqlite": sqlite_health,
        "qwen": doc_health["reachable"],
        "qwen_chat": chat_health["reachable"],
        "chroma": chroma_health,
    }
