"""业务层 — AI 状态检查。"""

from backend.core.qwen_client import check_health
from backend.core.config import QWEN_BASE_URL


async def get_status() -> dict:
    reachable = await check_health()
    return {
        "qwen_reachable": reachable,
        "qwen_url": QWEN_BASE_URL,
        "message": "Qwen 在线" if reachable else "Qwen推理引擎正在处理其它请求，请稍后再试",
    }
