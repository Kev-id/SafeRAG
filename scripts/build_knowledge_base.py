"""知识库重建脚本 — 按 SQLite 登记册重建 ChromaDB 索引。

用法:
    python scripts/build_knowledge_base.py [--force]

定位:
    - 日常上传/删除走 API（knowledge_service），本脚本平时不需要跑
    - 仅在索引损坏 / 需要整体重切时使用
    - 权威源是 SQLite kb_files 表，ChromaDB 是派生索引，从主库重建
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

# 保证 `python scripts/build_knowledge_base.py` 直接跑也能 import backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.chunker import read_text, file_md5
from backend.core.config import KB_SOURCE_DIR
from backend.core.kb_store import get_collection, upsert_file_chunks, delete_file_chunks
from backend.core.legal_parser import iter_legal_chunks, parse_to_tree
from backend.repositories import kb_file_repo, kb_tree_repo

logger = logging.getLogger(__name__)


def build(source_dir: str = KB_SOURCE_DIR, force: bool = False) -> dict:
    """按登记册重建索引：读 kb_files → 对每个文件判 md5 → 变了才重切。

    返回汇总 dict: {"chunks", "files", "updated", "skipped"}。
    """
    registered = kb_file_repo.list_files()
    if not registered:
        print("登记册为空，跳过（请先用上传接口添加文件）")
        return {"chunks": 0, "files": 0, "updated": 0, "skipped": 0}

    updated, skipped = 0, 0
    active_sources: set[str] = set()

    for kf in registered:
        filename = kf["filename"]
        active_sources.add(filename)

        path = os.path.join(source_dir, filename)
        if not os.path.isfile(path):
            logger.warning("登记文件 %s 不在源目录，跳过（先用删除接口清理）", filename)
            continue

        text = read_text(path)
        md5 = file_md5(text)

        # 没变就跳过（登记册的 md5 是真源，不用拉 ChromaDB）
        if not force and kf.get("md5") == md5:
            skipped += 1
            continue

        # 重建脚本以源文件为准：现解析文本成树（与登记 md5 一并对齐），存合同树，再切
        tree = parse_to_tree(text, source=filename, file_type=kf.get("file_type") or "")
        kb_tree_repo.save(filename, tree, md5)
        chunks, metadatas = iter_legal_chunks(tree)
        chunk_count = upsert_file_chunks(filename, chunks, md5, metadatas)
        kb_file_repo.upsert({
            "filename": filename, "md5": md5,
            "size": os.path.getsize(path), "chunk_count": chunk_count,
            "status": "ready", "message": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        updated += 1
        logger.info("已重建 %s（%d 块）", filename, chunk_count)

    # 清理：ChromaDB 里有、登记册里没有的孤儿块（索引损坏恢复）
    collection = get_collection()
    metas = collection.get(include=["metadatas"])["metadatas"]
    indexed_sources = {m["source"] for m in metas if "source" in m}
    for src in indexed_sources - active_sources:
        delete_file_chunks(src)
        logger.info("清理孤儿块: %s", src)

    final_metas = collection.get(include=["metadatas"])["metadatas"]
    file_count = len({m["source"] for m in final_metas})
    print(f"\n✅ 知识库重建完成: 共 {len(final_metas)} 块 / {file_count} 个文件（更新 {updated}，跳过 {skipped}）")

    # 自测：查一条，验证端到端能检索
    test_q = "企业应当如何开展安全生产教育和培训？"
    try:
        res = collection.query(query_texts=[test_q], n_results=3)
        print(f"\n自测查询: {test_q}")
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            print(f"  [{meta['source']}#{meta['chunk']}] (距离={dist:.4f}) {doc[:40]}...")
    except Exception:
        logger.exception("自测查询失败（不影响重建结果）")

    return {
        "chunks": len(final_metas),
        "files": file_count,
        "updated": updated,
        "skipped": skipped,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

    parser = argparse.ArgumentParser(description="按登记册重建知识库索引")
    parser.add_argument("--force", action="store_true", help="忽略 md5，全量重切所有登记文件")
    parser.add_argument("--source-dir", default=KB_SOURCE_DIR, help="源目录（默认取配置）")
    args = parser.parse_args()

    build(source_dir=args.source_dir, force=args.force)
