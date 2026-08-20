"""业务层 — AI 状态检查。"""

from backend.core.qwen_client import check_engines
from backend.core.config import QWEN_BASE_URLS


async def get_status() -> dict:
    engines = await check_engines()
    reachable = [e for e in engines if e["reachable"]]

    # qwen_url 保持 = 第一个引擎（旧字段，不破坏前端）；qwen_urls 是新增的完整列表
    if not reachable:
        return {
            "qwen_reachable": False,
            "qwen_busy": None,
            "qwen_url": QWEN_BASE_URLS[0],
            "qwen_urls": QWEN_BASE_URLS,
            "message": "Qwen 推理引擎不可达，请确认引擎已启动",
        }

    any_busy = any(e["busy"] for e in reachable)
    if any_busy:
        busy_count = sum(1 for e in reachable if e["busy"])
        return {
            "qwen_reachable": True,
            "qwen_busy": True,
            "qwen_url": QWEN_BASE_URLS[0],
            "qwen_urls": QWEN_BASE_URLS,
            "message": f"{len(reachable)}/{len(QWEN_BASE_URLS)} 台 Qwen 引擎在线，{busy_count} 台正在推理",
        }
    return {
        "qwen_reachable": True,
        "qwen_busy": False,
        "qwen_url": QWEN_BASE_URLS[0],
        "qwen_urls": QWEN_BASE_URLS,
        "message": f"{len(reachable)}/{len(QWEN_BASE_URLS)} 台 Qwen 引擎在线",
    }
