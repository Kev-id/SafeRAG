"""业务层 — AI 状态检查。"""

from backend.core.qwen_client import check_health
from backend.core.config import QWEN_BASE_URL


async def get_status() -> dict:
    health = await check_health()

    if not health["reachable"]:
        return {
            "qwen_reachable": False,
            "qwen_busy": None,
            "qwen_url": QWEN_BASE_URL,
            "message": "Qwen 推理引擎不可达，请确认引擎已启动",
        }
    if health["busy"]:
        return {
            "qwen_reachable": True,
            "qwen_busy": True,
            "qwen_url": QWEN_BASE_URL,
            "message": "Qwen 推理引擎正在处理其它请求",
        }
    return {
        "qwen_reachable": True,
        "qwen_busy": False,
        "qwen_url": QWEN_BASE_URL,
        "message": "Qwen 在线",
    }
