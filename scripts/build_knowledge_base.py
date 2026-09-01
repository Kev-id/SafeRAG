"""知识库重建脚本 — 按 SQLite 登记册重建 ChromaDB 索引。

用法:
    python scripts/build_knowledge_base.py [--force] [--workers N]

两段式（多进程并行嵌入，把 4.7 万块的 embedding 从单进程压到多进程跑）：
    阶段1 解析+切块（单进程，快）：读源文件 → 树 → 出块，登记跳过/失败
    阶段2 并行嵌入（多进程）：所有块按批分发到 N 个子进程 embed
    阶段3 写回索引（单进程）：用阶段2 的向量直插 Chroma，更新登记册

定位:
    - 日常上传/删除走 API（knowledge_service），本脚本平时不需要跑
    - 仅在索引损坏 / 需要整体重切时使用
    - 权威源是 SQLite kb_files 表，ChromaDB 是派生索引，从主库重建
"""

import argparse
import logging
import multiprocessing as mp
import os
import sys
import time
from datetime import datetime, timezone

# 保证 `python scripts/build_knowledge_base.py` 直接跑也能 import backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.chunker import file_md5
from backend.core.config import KB_SOURCE_DIR
from backend.core.kb_store import get_all_batch, get_collection, upsert_file_chunks, delete_file_chunks
from backend.core.legal_parser import extract_text, iter_legal_chunks, parse_to_tree
from backend.repositories import kb_file_repo, kb_tree_repo

logger = logging.getLogger(__name__)

# 并行嵌入：一个工作进程一次处理的文本条数。别太大——_encode_batch 按批内最长文本
# pad，真实法规块都 400+ 字，批 256 会让单个中间缓冲区冲到 2GB+，4 进程同时就 OOM。
_EMBED_BATCH = 128


def _md5_of(path: str, filename: str) -> str:
    """读源文件按格式提取文本算 md5（轻量跳过判据）。与 parse_to_tree 内算的同源同值。"""
    with open(path, "rb") as f:
        return file_md5(extract_text(f.read(), filename))


def _init_worker(n_workers: int) -> None:
    """子进程初始化：限制 ORT 线程数，避免 N 个进程互相超线程争抢 16 核。"""
    threads = max(1, os.cpu_count() // n_workers)
    os.environ["OMP_NUM_THREADS"] = str(threads)


def _embed_worker(texts: list[str]) -> list[list[float]]:
    """并行嵌入的子进程执行体：一次嵌入一批文本，返回向量列表（可 picklable）。

    失败自动拆半重试：个别超长/异常块让 ORT 分配失败时，拆成更小的批直到嵌入成功，
    避免一个批次把整个并行管道打爆。
    """
    from backend.core.embedding_client import embed
    try:
        return embed(texts).tolist()
    except Exception:
        if len(texts) <= 1:
            raise
        logger.warning("单批嵌入失败（%d 条），拆半重试", len(texts))
        mid = (len(texts) + 1) // 2
        out: list[list[float]] = []
        for part in (texts[:mid], texts[mid:]):
            out.extend(_embed_worker(part))
        return out


def _mark_failed(kf: dict, path: str, message: str) -> None:
    """把单个文件登记为 failed（不中断整批）。"""
    kb_file_repo.upsert({
        "filename": kf["filename"],
        # 关键：必须带上 file_type/region/city —— upsert 的 ON CONFLICT 会把这些字段
        # 用传进来的值覆盖，不带就会把登记册里的类型/省市全冲空（历史 bug）。
        "file_type": kf.get("file_type") or "",
        "region": kf.get("region") or "",
        "city": kf.get("city") or "",
        "size": os.path.getsize(path), "chunk_count": 0,
        "status": "failed", "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def build(source_dir: str = KB_SOURCE_DIR, force: bool = False,
          workers: int = 4) -> dict:
    """按登记册重建索引。返回 {"chunks", "files", "updated", "skipped", "failed"}。"""
    registered = kb_file_repo.list_files()
    if not registered:
        print("登记册为空，跳过（请先用上传接口添加文件）")
        return {"chunks": 0, "files": 0, "updated": 0, "skipped": 0, "failed": 0}

    updated = skipped = failed = 0
    active_sources: set[str] = set()

    # ------------------------------------------------------------------
    # 阶段1：解析 + 切块（单进程，约 0.5 分钟）。失败单文件标记 failed 跳过。
    # ------------------------------------------------------------------
    print(f"｜阶段 1/3｜解析切片 {len(registered)} 个文件…")
    items = []  # (filename, md5, path, chunks, metadatas)
    for kf in registered:
        filename = kf["filename"]
        active_sources.add(filename)
        path = os.path.join(source_dir, filename)
        if not os.path.isfile(path):
            logger.warning("登记文件 %s 不在源目录，跳过（先用删除接口清理）", filename)
            continue
        try:
            # 没变就跳过（登记册的 md5 是真源，不用拉 ChromaDB）。
            md5 = _md5_of(path, filename)
            if not force and kf.get("md5") == md5:
                skipped += 1
                continue
            with open(path, "rb") as f:
                content = f.read()
            tree, md5 = parse_to_tree(
                content, filename=filename, file_type=kf.get("file_type") or "",
                region=kf.get("region") or "",
                city=kf.get("city") or "",
            )
            kb_tree_repo.save(filename, tree, md5)
            chunks, metadatas = iter_legal_chunks(tree)
        except Exception as e:
            logger.exception("解析失败，跳过 %s", filename)
            _mark_failed(kf, path, str(e))
            failed += 1
            continue
        items.append((filename, md5, path, chunks, metadatas))

    total_chunks = sum(len(it[3]) for it in items)
    if not items:
        print("｜阶段1｜没有需要处理的文件（全部跳过或失败），直接收尾。")
    else:
        # ------------------------------------------------------------------
        # 阶段2：并行 embedding（多进程，耗时大头在这）
        # ------------------------------------------------------------------
        print(f"｜阶段 2/3｜并行嵌入 {total_chunks} 块 × {workers} 进程…")
        texts: list[str] = []
        starts: list[int] = []
        for fn, md5, path, chunks, metas in items:
            starts.append(len(texts))
            texts.extend(chunks)

        batches = [texts[i:i + _EMBED_BATCH]
                   for i in range(0, len(texts), _EMBED_BATCH)]
        flat_emb: list[list[float]] = []
        t0 = time.monotonic()
        with mp.Pool(workers, initializer=_init_worker, initargs=(workers,)) as pool:
            done = 0
            for vecs in pool.imap(_embed_worker, batches):
                flat_emb.extend(vecs)
                done += len(vecs)
                if done % (workers * _EMBED_BATCH) < _EMBED_BATCH or done >= len(texts):
                    el = time.monotonic() - t0
                    print(f"   嵌入 {done}/{len(texts)} 块 | 已用 {el:.0f}s | {done/el:.0f} 条/s",
                          flush=True)
        print(f"｜阶段 2/3｜嵌入完成（{time.monotonic()-t0:.0f}s）")

        # ------------------------------------------------------------------
        # 阶段3：按文件写回 Chroma（单进程，用预计算向量直插）
        # ------------------------------------------------------------------
        print("｜阶段 3/3｜写回 Chroma 索引…")
        for idx, (filename, md5, path, chunks, metadatas) in enumerate(items):
            s = starts[idx]
            emb = flat_emb[s:s + len(chunks)]
            m0 = metadatas[0] if metadatas else {}
            # file_type/region/city 从该文件切块的 metadata 取（解析阶段已固化进块）
            kf = {
                "filename": filename,
                "file_type": m0.get("file_type") or "",
                "region": m0.get("region") or "",
                "city": m0.get("city") or "",
            }
            try:
                chunk_count = upsert_file_chunks(filename, chunks, md5, metadatas, embeddings=emb)
            except Exception as e:
                logger.exception("写库失败，跳过 %s", filename)
                _mark_failed(kf, path, str(e))
                failed += 1
                continue
            kb_file_repo.upsert({
                "filename": filename, "md5": md5,
                # 关键：必须带上 file_type/region/city —— upsert 的 ON CONFLICT 会把这些字段
                # 用传进来的值覆盖，不带就会把登记册里的类型/省市全冲空（历史 bug）。
                "file_type": kf["file_type"],
                "region": kf["region"],
                "city": kf["city"],
                "size": os.path.getsize(path), "chunk_count": chunk_count,
                "status": "ready", "message": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            updated += 1
            logger.info("已写入 %s（%d 块）", filename, chunk_count)

    # ------------------------------------------------------------------
    # 清理：ChromaDB 里有、登记册里没有的孤儿块（索引损坏恢复）
    # ------------------------------------------------------------------
    collection = get_collection()
    metas = get_all_batch(collection, include=["metadatas"])["metadatas"]
    indexed_sources = {m["source"] for m in metas if "source" in m}
    for src in indexed_sources - active_sources:
        delete_file_chunks(src)
        logger.info("清理孤儿块: %s", src)

    final_metas = get_all_batch(collection, include=["metadatas"])["metadatas"]
    file_count = len({m["source"] for m in final_metas})
    print(f"\n✅ 知识库重建完成: 共 {len(final_metas)} 块 / {file_count} 个文件"
          f"（更新 {updated}，跳过 {skipped}，失败 {failed}）")

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
        "failed": failed,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")

    parser = argparse.ArgumentParser(description="按登记册重建知识库索引")
    parser.add_argument("--force", action="store_true", help="忽略 md5，全量重切所有登记文件")
    parser.add_argument("--source-dir", default=KB_SOURCE_DIR, help="源目录（默认取配置）")
    parser.add_argument("--workers", type=int, default=4, help="并行嵌入进程数（默认4，16核建议4~6）")
    args = parser.parse_args()

    build(source_dir=args.source_dir, force=args.force, workers=args.workers)