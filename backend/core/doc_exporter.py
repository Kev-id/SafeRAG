"""报告导出 — 用 pandoc 把 Markdown 转 Word 等格式。

pandoc 是命令行工具，用 asyncio 子进程异步调用，不阻塞事件循环。
Word 是按需派生的：报告 .md 是唯一真相，.docx 只在下载时临时生成。

PANDOC_PATH 从环境变量读（默认 "pandoc" 走 PATH），不依赖 config.py——
因为 config.py 可能因本机跳过跟踪而缺少该配置，且 pandoc 路径本就不是
部署核心配置，环境变量足够。
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# pandoc 可执行路径：环境变量覆盖，默认走 PATH
PANDOC_PATH = os.getenv("PANDOC_PATH", "pandoc")

# 转换超时（秒）：pandoc 一般秒级，防大文件卡死请求
_CONVERT_TIMEOUT = 60


async def md_to_docx(md_path: str, docx_path: str) -> None:
    """调 pandoc 把 .md 转 .docx。

    Raises:
        RuntimeError: pandoc 不存在 / 转换失败 / 超时。
    """
    cmd = [PANDOC_PATH, md_path, "-o", docx_path]
    logger.info("导出 Word: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CONVERT_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError("pandoc 转换超时")
    except FileNotFoundError:
        raise RuntimeError(f"找不到 pandoc（{PANDOC_PATH}），请确认已安装或设置 PANDOC_PATH")
    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc 失败: {stderr.decode(errors='replace')[:300]}"
        )
