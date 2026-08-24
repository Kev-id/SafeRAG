"""业务层 — AI 状态检查（文档引擎 + 聊天引擎）。"""

from backend.core.qwen_client import check_health, check_chat_health
from backend.core.config import QWEN_DOC_URL, QWEN_CHAT_URL


async def get_status() -> dict:
    doc = await check_health()
    chat = await check_chat_health()

    doc_ok, chat_ok = doc["reachable"], chat["reachable"]

    if not doc_ok and not chat_ok:
        return {
            "qwen_reachable": False,
            "qwen_busy": None,
            "qwen_url": QWEN_DOC_URL,
            "qwen_chat_reachable": False,
            "qwen_chat_busy": None,
            "qwen_chat_url": QWEN_CHAT_URL,
            "message": "文档引擎和聊天引擎都不可达，请确认引擎已启动",
        }

    if not doc_ok:
        return {
            "qwen_reachable": False,
            "qwen_busy": None,
            "qwen_url": QWEN_DOC_URL,
            "qwen_chat_reachable": True,
            "qwen_chat_busy": chat["busy"],
            "qwen_chat_url": QWEN_CHAT_URL,
            "message": "文档引擎不可达，聊天引擎在线",
        }

    if not chat_ok:
        return {
            "qwen_reachable": True,
            "qwen_busy": doc["busy"],
            "qwen_url": QWEN_DOC_URL,
            "qwen_chat_reachable": False,
            "qwen_chat_busy": None,
            "qwen_chat_url": QWEN_CHAT_URL,
            "message": "文档引擎在线，聊天引擎不可达",
        }

    return {
        "qwen_reachable": True,
        "qwen_busy": doc["busy"],
        "qwen_url": QWEN_DOC_URL,
        "qwen_chat_reachable": True,
        "qwen_chat_busy": chat["busy"],
        "qwen_chat_url": QWEN_CHAT_URL,
        "message": "双 Qwen 引擎在线",
    }