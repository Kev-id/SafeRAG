"""业务层 — 知识库文件管理。

权威源（Master）：SQLite kb_files 登记表 + 源目录磁盘文件。
派生索引：ChromaDB（只服务检索）。

每个操作都是"正向写两处"，不存在对账：
    上传: 写磁盘 → 登记(building) → 切块写索引 → 更新(ready)
    删除: 删索引块 → 删磁盘 → 删登记行
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from backend.core.config import KB_SOURCE_DIR
from backend.core.retriever import reset_retriever
from backend.core.chunker import decode_text, file_md5, read_text, split_text
from backend.core.kb_store import delete_file_chunks, upsert_file_chunks
from backend.repositories import kb_file_repo

logger = logging.getLogger(__name__)

# 单文件上传大小上限（防误传大文件撑爆磁盘）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB


async def upload_kb_file(filename: str, content: bytes) -> dict:
    """保存上传的 txt，登记到 SQLite，并把切块写入 ChromaDB 索引。"""
    safe_name = os.path.basename(filename)  # 防路径穿越：只取文件名
    if not safe_name.lower().endswith(".txt"):
        raise ValueError(f"仅支持 .txt 格式，收到: {safe_name}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 限制: {safe_name}")

    os.makedirs(KB_SOURCE_DIR, exist_ok=True)
    file_path = os.path.join(KB_SOURCE_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(content)

    now = datetime.now(timezone.utc).isoformat()
    try:
        text = decode_text(content)
    except ValueError as e:
        # 编码无法识别 → 删掉刚写的文件，登记 failed
        os.remove(file_path)
        kb_file_repo.upsert({
            "filename": safe_name, "status": "failed",
            "message": "无法识别文件编码", "updated_at": now,
        })
        raise ValueError(f"无法识别文件编码: {safe_name}") from e

    md5 = file_md5(text)
    kb_file_repo.upsert({
        "filename": safe_name, "md5": md5, "size": len(content),
        "chunk_count": 0, "status": "building", "message": None, "updated_at": now,
    })

    try:
        chunks = split_text(text)
        # 切块 + embedding 是 CPU 密集，丢线程池跑，别卡事件循环
        chunk_count = await asyncio.to_thread(upsert_file_chunks, safe_name, chunks, md5)
    except Exception:
        logger.exception("写入知识库索引失败: %s", safe_name)
        kb_file_repo.upsert({
            "filename": safe_name, "md5": md5, "size": len(content),
            "chunk_count": 0, "status": "failed", "message": "写入索引失败", "updated_at": now,
        })
        raise

    kb_file_repo.upsert({
        "filename": safe_name, "md5": md5, "size": len(content),
        "chunk_count": chunk_count, "status": "ready", "message": None, "updated_at": now,
    })
    reset_retriever()  # 检索器缓存失效，下次检索才拿得到新文件
    return {"message": f"已上传并重建知识库: {safe_name}（{chunk_count} 块）"}


async def delete_kb_file(filename: str) -> dict:
    """删除知识库文件：删索引块 → 删磁盘 → 删登记行。"""
    safe_name = os.path.basename(filename)

    if kb_file_repo.get(safe_name) is None:
        raise FileNotFoundError(f"文件不存在: {safe_name}")

    await asyncio.to_thread(delete_file_chunks, safe_name)  # ① 删 ChromaDB 块

    file_path = os.path.join(KB_SOURCE_DIR, safe_name)
    if os.path.isfile(file_path):
        os.remove(file_path)  # ② 删磁盘

    kb_file_repo.delete(safe_name)  # ③ 删登记行
    reset_retriever()
    return {"message": f"已删除并重建知识库: {safe_name}"}


async def list_kb_files() -> list[dict]:
    """列出知识库文件（直接读登记册，不碰 ChromaDB）。"""
    return kb_file_repo.list_all()


async def get_kb_file(filename: str) -> dict:
    """获取单个文件：登记册元数据 + 源目录正文。"""
    safe_name = os.path.basename(filename)
    kf = kb_file_repo.get(safe_name)
    if kf is None:
        raise FileNotFoundError(f"文件不存在: {safe_name}")

    # 正文从源目录磁盘读（登记册只存元数据）
    file_path = os.path.join(KB_SOURCE_DIR, safe_name)
    if os.path.isfile(file_path):
        try:
            kf["content"] = read_text(file_path)
        except ValueError:
            logger.warning("读取文件正文失败（编码问题）: %s", safe_name)
            kf["content"] = None
    else:
        kf["content"] = None
    return kf
