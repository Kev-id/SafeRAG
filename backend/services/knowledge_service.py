"""业务层 — 知识库文件管理。

权威源（Master）：SQLite kb_files 登记表 + kb_trees 文档树表 + 源目录磁盘文件。
派生索引：ChromaDB（只服务检索）。

解析与入库两段式（tree.json 是活合同）：
    上传: 写磁盘 → 登记(building) → 解析成树存 kb_trees → 从树出块写索引 → 更新(ready)
    删除: 删索引块 → 删磁盘 → 删登记行 → 删树行
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from backend.core.config import KB_SOURCE_DIR
from backend.core.retriever import reset_retriever
from backend.core.chunker import decode_text, file_md5, read_text
from backend.core.kb_store import delete_file_chunks, upsert_file_chunks
from backend.core.legal_parser import iter_legal_chunks, parse_to_tree
from backend.repositories import kb_file_repo, kb_tree_repo

logger = logging.getLogger(__name__)

# 单文件上传大小上限（防误传大文件撑爆磁盘）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB


async def upload_kb_file(filename: str, content: bytes, file_type: str) -> dict:
    """保存上传的 txt，登记到 SQLite，解析成文档树，再从树切块写入索引。"""
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
        "filename": safe_name, "md5": md5, "file_type": file_type, "size": len(content),
        "chunk_count": 0, "status": "building", "message": None, "updated_at": now,
    })

    try:
        # 解析段：文本 → 文档树（合同），CPU 密集丢线程池，落盘到 kb_trees
        tree = await asyncio.to_thread(parse_to_tree, text, safe_name, file_type)
        await asyncio.to_thread(kb_tree_repo.save, safe_name, tree, md5)
        # 入库段：只认树，从树出块 → 写索引，绝不重新解析
        chunks, metadatas = iter_legal_chunks(tree)
        chunk_count = await asyncio.to_thread(
            upsert_file_chunks, safe_name, chunks, md5, metadatas
        )
    except Exception:
        logger.exception("写入知识库索引失败: %s", safe_name)
        kb_file_repo.upsert({
            "filename": safe_name, "md5": md5, "file_type": file_type, "size": len(content),
            "chunk_count": 0, "status": "failed", "message": "写入索引失败", "updated_at": now,
        })
        raise

    kb_file_repo.upsert({
        "filename": safe_name, "md5": md5, "file_type": file_type, "size": len(content),
        "chunk_count": chunk_count, "status": "ready", "message": None, "updated_at": now,
    })
    reset_retriever()  # 检索器缓存失效，下次检索才拿得到新文件
    return {"message": f"已上传并重建知识库: {safe_name}（{chunk_count} 块）"}


async def delete_kb_file(filename: str) -> dict:
    """删除知识库文件：删索引块 → 删磁盘 → 删登记行 → 删树行。"""
    safe_name = os.path.basename(filename)

    if kb_file_repo.get(safe_name) is None:
        raise FileNotFoundError(f"文件不存在: {safe_name}")

    await asyncio.to_thread(delete_file_chunks, safe_name)  # ① 删 ChromaDB 块

    file_path = os.path.join(KB_SOURCE_DIR, safe_name)
    if os.path.isfile(file_path):
        os.remove(file_path)  # ② 删磁盘

    kb_file_repo.delete(safe_name)  # ③ 删登记行
    kb_tree_repo.delete(safe_name)  # ④ 删树行
    reset_retriever()
    return {"message": f"已删除并重建知识库: {safe_name}"}


async def list_kb_files(file_type:Optional[str]=None, status: Optional[str]=None, keyword: Optional[str]=None) -> list[dict]:
    """列出知识库文件（直接读登记册，不碰 ChromaDB）。"""
    return kb_file_repo.list_files(file_type=file_type, status=status, keyword=keyword)


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

def get_stats() -> dict:
    """获取知识库统计信息"""
    return  kb_file_repo.get_stats()
