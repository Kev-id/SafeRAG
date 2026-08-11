"""业务层 — AI 状态检查。"""

from backend.qwen_client import check_health


async def get_status() -> dict:
    reachable = await check_health()
    return {
        "qwen_reachable": reachable,
        "qwen_url": "http://127.0.0.1:8000",
        "message": "Qwen 在线" if reachable else "无法连接 Qwen 推理引擎",
    }
