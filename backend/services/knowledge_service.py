import asyncio
import logging
import os

from backend.core.config import KB_SOURCE_DIR
from backend.core.retriever import reset_retriever
from scripts.build_knowledge_base import build

logger = logging.getLogger(__name__)#这行意思是获取当前模块的日志记录器

async def upload_kb_file(filename: str, content: bytes) -> dict:
    """保存上传的 txt 并增量重建知识库索引。

    返回 build() 的message。
    """
    safe_name = os.path.basename(filename)# 获取文件名，避免路径遍历攻击
    if not safe_name.lower().endswith(".txt"):
        raise ValueError(f"仅支持 .txt 格式，收到: {safe_name}")
    os.makedirs(KB_SOURCE_DIR, exist_ok=True)# 创建知识库源文件目录，如果不存在则创建
    file_path = os.path.join(KB_SOURCE_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(content)# 将上传的文件内容写入到指定路径

    await asyncio.to_thread(build, KB_SOURCE_DIR)# 异步调用 build() 函数，重建知识库索引。
    reset_retriever()# 重置检索器，确保下次检索时加载最新的知识库

    return {"message": f"已上传并重建知识库: {safe_name}"}


