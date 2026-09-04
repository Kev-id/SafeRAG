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
from backend.core.chunker import read_text
from backend.core.kb_store import delete_file_chunks, upsert_file_chunks
from backend.core.legal_parser import extract_text, iter_legal_chunks, parse_to_tree
from backend.repositories import kb_file_repo, kb_tree_repo

logger = logging.getLogger(__name__)

# 单文件上传大小上限（防误传大文件撑爆磁盘）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB

# 允许的文件后缀（新格式在 legal_parser.extract_text 加提取器后，同步放开这里即可）
ALLOWED_EXTS = {".txt", ".docx", ".pdf"}

_wake_kb = asyncio.Event()

async def upload_kb_file(filename: str, content: bytes, file_type: str,
                         region: str | None = None, city: str | None = None) -> dict:
    """保存上传文件，登记到 SQLite。

    文件格式由 parse_to_tree 按 filename 后缀分派，本函数不关心格式。
    region/city：省/直辖市 + 地级市（前端传，空=全国性法规）。"""
    safe_name = os.path.basename(filename)  # 防路径穿越：只取文件名
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"暂不支持的格式 {ext or '（无后缀）'}，当前支持: {sorted(ALLOWED_EXTS)}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 限制: {safe_name}")

    os.makedirs(KB_SOURCE_DIR, exist_ok=True)
    file_path = os.path.join(KB_SOURCE_DIR, safe_name)
    with open(file_path, "wb") as f:
        f.write(content)

    now = datetime.now(timezone.utc).isoformat()
    kb_file_repo.upsert({
        "filename": safe_name, "file_type": file_type, "region": region, "city": city,
        "size": len(content),
        "chunk_count": 0, "status": "queued", "message": None, "updated_at": now,
    })
    _wake_kb.set()
    return {"message": f"已上传文件: {safe_name}，正在解析入库，请稍后查询状态。"}

async def _process_kb_file(kf: dict) -> dict:
    """解析成文档树，再从树切块写入索引。"""
    safe_name = kf["filename"]
    file_type = kf["file_type"]
    region = kf["region"]
    city = kf["city"] if "city" in kf else ""
    with open(os.path.join(KB_SOURCE_DIR, safe_name), "rb") as f:
        content = f.read()

    try:
        # 解析段：bytes → 文档树（合同），CPU 密集丢线程池，落盘到 kb_trees
        tree, md5 = await asyncio.to_thread(
            parse_to_tree, content, safe_name, file_type, region, city
        )
        await asyncio.to_thread(kb_tree_repo.save, safe_name, tree, md5)
        # 入库段：只认树，从树出块 → 写索引，绝不重新解析
        chunks, metadatas = iter_legal_chunks(tree)
        chunk_count = await asyncio.to_thread(
            upsert_file_chunks, safe_name, chunks, md5, metadatas
        )
    except Exception as e:
        logger.exception("写入知识库索引失败: %s", safe_name)
        now = datetime.now(timezone.utc).isoformat()
        # 关键：失败分支也必须带上 region/city —— upsert 的 ON CONFLICT 会把登记册
        # 字段用传进来的值覆盖，漏带就会把省市冲空（历史 bug，见 build_knowledge_base.py）
        kb_file_repo.upsert({
            "filename": safe_name, "file_type": file_type, "region": region,
            "city": city, "size": len(content),
            "chunk_count": 0, "status": "failed", "message": str(e), "updated_at": now,
        })
        raise
    now = datetime.now(timezone.utc).isoformat()
    kb_file_repo.upsert({
        "filename": safe_name, "md5": md5, "file_type": file_type, "region": region,
        "city": city, "size": len(content),
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


async def list_kb_files(file_type:Optional[str]=None, status: Optional[str]=None, keyword: Optional[str]=None, region: Optional[str]=None, city: Optional[str]=None) -> list[dict]:
    """列出知识库文件（直接读登记册，不碰 ChromaDB）。"""
    return kb_file_repo.list_files(file_type=file_type, status=status, keyword=keyword, region=region, city=city)


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
            with open(file_path, "rb") as f:
                kf["content"] = extract_text(f.read(), safe_name)
        except ValueError:
            logger.warning("读取文件正文失败: %s", safe_name)
            kf["content"] = None
    else:
        kf["content"] = None
    return kf

def get_stats() -> dict:
    """获取知识库统计信息"""
    return  kb_file_repo.get_stats()


async def worker() -> None:
    """常驻消费协程：认领下一条 building → 解析 → 入库 → 取下一条。

    单协程天然串行，替代原来的 asyncio.Lock。没任务时睡在 _wake 上，
    被新任务唤醒；事件丢失也不影响——表里的任务下一轮必被认领。
    """
    logger.info("知识库文件处理 worker 已启动")
    while True:
        kf = kb_file_repo.claim_next()
        if kf is None:
            await _wake_kb.wait()
            _wake_kb.clear()
            continue
        try:
            await _process_kb_file(kf)
        except Exception:
            logger.exception("处理知识库文件失败: %s", kf["filename"])