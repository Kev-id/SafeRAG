"""检索评估脚本（命令行，单配置一次跑完）。

用法：
    python scripts/eval_retrieval.py --queries scripts/eval_queries.auto.json \
        --model 混合检索v2 --use-filter --top-k 5 --out eval_report.txt

  --model       本次用的模型/配置名（写进报告头，多模型对比时区分）
  --use-filter  开地区+类型筛选；不带则不开
  --out         .txt 输出路径（追加写，跑多轮会累积）

输出内容：模型、筛选开关、top_k/题数、以及按任务类型（全部/短问句/事故简报/长文）的
hit@k / recall@k / prec@k / MRR。
"""

import argparse
import json
import logging
import os
import random
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)


def _metrics(results: list[list[str]], golds: list[set[str]], k: int) -> dict:
    """results[i]=第 i 问 top-k 来源；golds[i]=期望来源集合。返回四项均值。"""
    n = len(results)
    hit = rec = prec = mrr = 0.0
    for ret, g in zip(results, golds):
        s = set(ret)
        if not g:
            continue
        hit += bool(g & s)
        rec += len(g & s) / len(g)
        prec += len(g & s) / k
        rank = next((j + 1 for j, x in enumerate(ret) if x in g), None)
        mrr += (1.0 / rank) if rank else 0.0
    return {"n": n, "hit": hit / n, "rec": rec / n, "prec": prec / n, "mrr": mrr / n} if n else \
        {"n": 0, "hit": 0.0, "rec": 0.0, "prec": 0.0, "mrr": 0.0}


def build_report(queries: list[dict], results: list[list[str]], k: int,
                 model: str, use_filter: bool) -> str:
    """拼一段 txt 报告：模型 / 筛选开关 / 分任务指标。"""
    golds = [set(q["gold"]) for q in queries]
    splits = [("全部", None)] + [
        (("短问句", "short"), ("事故简报", "news"), ("长文(≥500字)", "long"))[i]
        for i in range(3)
    ]  # ("显示名", kind)；kind=None 表示全部题
    lines = []
    lines.append("=" * 52)
    lines.append(f"时间:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"模型:    {model}")
    lines.append(f"地区筛选: {'开' if use_filter else '关'}")
    lines.append(f"top_k:   {k} | 题数: {len(queries)}")
    lines.append("-" * 52)
    lines.append(f"{'任务类型':<12}{'题数':>5}{'hit@k':>9}{'recall@k':>10}{'prec@k':>9}{'MRR':>8}")
    for name, kind in splits:
        if kind is None:
            qs, rs = queries, results
        else:
            idx = [i for i, q in enumerate(queries) if q.get("kind") == kind]
            qs = [queries[i] for i in idx]
            rs = [results[i] for i in idx]
        if not qs:
            continue
        m = _metrics(rs, [set(q["gold"]) for q in qs], k)
        lines.append(f"{name:<12}{m['n']:>5}{m['hit']:>9.4f}{m['rec']:>10.4f}"
                     f"{m['prec']:>9.4f}{m['mrr']:>8.4f}")
    lines.append("=" * 52)
    return "\n".join(lines)


def _retrieve_sources(retriever, q: dict, k: int, use_filter: bool) -> list[str]:
    if use_filter:
        hits = retriever.retrieve(
            q["q"], top_k=k,
            provinces=q.get("provinces") or None,
            cities=q.get("cities") or None,
            file_types=q.get("file_types") or None,
        )
    else:
        hits = retriever.retrieve(q["q"], top_k=k)
    return [h["meta"].get("source", "?") for h in hits]


def main() -> None:
    ap = argparse.ArgumentParser(description="检索质量评估（一次跑一个配置）")
    ap.add_argument("--queries", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "eval_results", "eval_queries.auto.json"))
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--model", default="未标注模型", help="本次模型/配置名")
    ap.add_argument("--use-filter", action="store_true", help="开地区+类型筛选")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "eval_results", "eval_report.txt"),
        help=".txt 输出路径（追加累积，默认在 scripts/eval_results/ 下）")
    ap.add_argument("--split", choices=["short", "news", "long", "all"], default="all")
    ap.add_argument("--sample", type=int, default=0, help="随机抽样 N 条（--seed 固定）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max", type=int, default=0, help="取前 N 条（0=全部）")
    ap.add_argument("--model-dir", default=None, help="embedding 模型目录（覆盖环境变量）")
    args = ap.parse_args()

    if args.model_dir:
        os.environ["EMBEDDING_MODEL_PATH"] = args.model_dir

    with open(args.queries, encoding="utf-8") as f:
        queries = json.load(f)
    if args.split != "all":
        queries = [q for q in queries if q.get("kind") == args.split]
    if args.sample:
        queries = list(random.Random(args.seed).sample(
            queries, min(args.sample, len(queries))))
    if args.max:
        queries = queries[: args.max]
    print(f"评测 {len(queries)} 条，top-{args.top_k}，"
          f"模型={args.model}，筛选={'开' if args.use_filter else '关'}")

    # 检索：懒加载一次
    from backend.core.retriever import get_retriever
    retriever = get_retriever()

    results: list[list[str]] = []
    n = len(queries)
    for i, q in enumerate(queries, 1):
        results.append(_retrieve_sources(retriever, q, args.top_k, args.use_filter))
        if i % 50 == 0 or i == n:
            print(f"  进度 {i}/{n}", flush=True)

    report = build_report(queries, results, args.top_k, args.model, args.use_filter)
    print(report)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(report + "\n\n")
        print(f"\n已追加到: {args.out}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()