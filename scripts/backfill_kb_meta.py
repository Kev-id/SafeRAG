"""一次性修库：从 Chroma 索引的 chunk 元数据回填 kb_files 被 build 冲空的元数据。

背景：build_knowledge_base 之前调 kb_file_repo.upsert 没带 file_type/region/city，
ON CONFLICT 更新时把它们全写成了空。正确值仍完整存在于（信息最多的）活跃集合的
chunk 元数据里 —— 按 source（文件名）取回，写回登记册。

用法：
    python scripts/backfill_kb_meta.py            # 实际回填
    python scripts/backfill_kb_meta.py --dry-run  # 只看要改多少，不动库

幂等：只更新"当前字段为空"的行；再跑也不会越修越坏。
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "backend", "data", "saferag.db")
KB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "backend", "data", "kb")


def load_collection_meta() -> dict[str, dict]:
    """从信息最全的集合构建 source → {file_type, region, city}。"""
    import chromadb
    client = chromadb.PersistentClient(path=KB_DIR)
    best: dict[str, dict] = {}
    for col in client.list_collections():
        col = client.get_collection(col.name)
        meta: dict[str, dict] = {}
        off = 0
        while True:
            batch = col.get(include=["metadatas"], limit=3000, offset=off)
            ids = batch.get("ids") or []
            if not ids:
                break
            for m in batch.get("metadatas") or []:
                s = m.get("source")
                if s and s not in meta:
                    meta[s] = {"file_type": m.get("file_type") or "",
                               "region": m.get("region") or "",
                               "city": m.get("city") or ""}
            off += len(ids)
            if len(ids) < 3000:
                break
        if len(meta) > len(best):
            print(f"  ← 选用集合 '{col.name}'（{len(meta)} 个 source，信息最全）")
            best = meta
    return best


def heuristic(filename: str) -> dict:
    """兜底：索引里没有时按文件名猜（只补 file_type）。"""
    if filename.startswith("中华人民共和国") or filename.endswith("法.txt"):
        return {"file_type": "国家法律", "region": "", "city": ""}
    return {"file_type": "地方法规", "region": "", "city": ""}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT filename, file_type, region, city FROM kb_files WHERE status='ready'"
    ).fetchall()

    broken = [r for r in rows
              if not (r["file_type"] or "") or not (r["region"] or "") or not (r["city"] or "")]
    print(f"登记册 ready {len(rows)} 行，其中元数据有空缺的 {len(broken)} 行")

    meta = load_collection_meta()
    fixed, cant = 0, []
    for r in broken:
        src = meta.get(r["filename"]) or heuristic(r["filename"])
        if not (src["file_type"]):
            cant.append(r["filename"])
            continue
        if not args.dry_run:
            conn.execute(
                "UPDATE kb_files SET file_type=?, region=?, city=? WHERE filename=?",
                (src["file_type"], src["region"], src["city"], r["filename"]),
            )
        fixed += 1
    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"回填 {fixed} 行（索引取回 + 文件名兜底）")
    if cant:
        print("仍无法确定类型的:", cant)
    if args.dry_run:
        print("（--dry-run：以上只是预览，未写库）")
    else:
        conn2 = sqlite3.connect(DB)
        for r in conn2.execute("SELECT file_type, COUNT(*) c FROM kb_files WHERE status='ready' GROUP BY file_type"):
            print(f"  [{r[0] or '(空)'}] {r[1]} 个")
        conn2.close()


if __name__ == "__main__":
    main()