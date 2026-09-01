# 检索评估测试 — 使用文档

对知识库检索打分的两段式工具：**先造查询集，再跑评估**。用于量化不同模型、不同筛选
配置下，混合检索（BM25 + 向量 + RRF）能否把"问题对应的法规文件"捞回 top-k。

## 文件清单（scripts/）

| 文件 | 作用 |
|---|---|
| `gen_eval_queries.py` | 从 kb_files 登记册**生成评估查询集**（短问句 / 事故简报 / 长文） |
| `eval_queries.auto.json` | 生成的查询集（脚本输出） |
| `eval_retrieval.py` | **跑评估**：对查询集检索一批、算出指标、写 .txt 报告 |
| `scripts\eval_results\eval_report.txt` | 评估报告（`--out` 指定路径，追加累积） |

## 流程总览

```
① 生成/刷新查询集            ② 跑评估（一个配置一次）      ③ 多模型对比
python gen_eval_queries.py → python eval_retrieval.py -… → 同一 --out 文件追加
```

## ① 生成查询集

```cmd
conda activate SafeRAG
cd /d D:\Users\Administrator\Desktop\SafeRAG

python scripts\gen_eval_queries.py                :: 全量生成（1040 个 ready 文件）
python scripts\gen_eval_queries.py --long-count 60 :: 长文（≥500字）生成多少条，默认40
python scripts\gen_eval_queries.py --out my.json    :: 换个输出文件名
```

默认输出 `scripts/eval_results/eval_queries.auto.json`，每条含：

```json
{
  "q": "三亚的烟花爆竹安全有哪些规定要求？",   // 检索提问
  "gold": ["三亚市烟花爆竹燃放安全管理规定.docx"], // 期望命中的来源文件
  "kind": "short",                        // short | news | long
  "file_types": ["地方法规"],
  "provinces": ["海南"],                   // gold 文档登记的地域，供筛选模式用
  "cities": ["三亚"]
}
```

**生成规则**：short 对所有 ready 文件生成（按文件名回声，天然相关）；news/long **只对
文件名能匹配领域词表的文件生成**——故障叙述按该领域编，保证"故事 ↔ gold"同领域，
不再出现"福建普通事故 → 期望《森林条例》"的错配样本。无领域匹配的文件只给 short
（生成时会打印"多少文件无领域匹配"）。

## ② 跑评估

```cmd
:: 不开筛选（基线）
python scripts\eval_retrieval.py --queries scripts\eval_results\eval_queries.auto.json --top-k 5 --model "模型A" --out scripts\eval_results\eval_report.txt --model-dir D:\Users\Administrator\Desktop\models\bge-small-zh-onnx

:: 开地区+类型筛选
python scripts\eval_retrieval.py --queries scripts\eval_results\eval_queries.auto.json --top-k 5 --model "模型A-筛选" --use-filter --out scripts\eval_results\eval_report.txt --model-dir D:\Users\Administrator\Desktop\models\bge-small-zh-onnx
```

**参数表**

| 参数 | 含义 | 默认 |
|---|---|---|
| `--queries` | 查询集 JSON | `scripts/eval_results/eval_queries.auto.json` |
| `--top-k` | 检索返回条数 | `5` |
| `--model` | 模型/配置名，写进报告头 | 未标注模型 |
| `--use-filter` | 开地区+类型筛选；**不带=关** | 关 |
| `--out` | .txt 输出路径（**追加**累积） | 不写文件 |
| `--split` | 只测某类：`short`/`news`/`long`/`all` | all |
| `--sample N` | 随机抽 N 条（`--seed` 固定可复现） | 0=全量 |
| `--seed` | 抽样种子 | 42 |
| `--max N` | 取前 N 条 | 0=全量 |
| `--model-dir` | embedding 模型目录（覆盖环境变量） | 环境变量 |

**本机提速**：全量约 1550 条跑全要 40 分钟以上，`--sample 60` 约 6 分钟出趋势，
`--split long`（40 条）约 4 分钟。进度每 50 条打一行。

**模型目录**：`--model-dir` 覆盖 `EMBEDDING_MODEL_PATH`。本机全局 env 可能指向盒子路径
（`/data2/models/...`），本机跑必带；真盒子上数据同步好可直接跑。

## ③ 报告与多模型对比

每次跑完，报告追加到 `--out` 文件（`========` 分隔，多轮累积，横向翻看）。

```
====================================================
时间:    2026-08-31 15:01:45
模型:    冒烟B-筛选开
地区筛选: 开
top_k:   5 | 题数: 3
----------------------------------------------------
任务类型           题数    hit@k  recall@k   prec@k     MRR
全部              3   0.3333    0.3333   0.0667  0.3333
短问句             1   1.0000    1.0000   0.2000  1.0000
事故简报            2   0.0000    0.0000   0.0000  0.0000
====================================================
```

**指标含义**（对每题 top_k 结果 vs 期望来源集合）：

| 指标 | 含义 |
|---|---|
| `hit@k` | 期望来源是否出现在 top-k（0/1 取平均）——"能不能找到" |
| `recall@k` | 期望来源命中了几个 / 期望总数——"找到几个" |
| `prec@k` | 命中数 / k——"返回里多少是想要的" |
| `MRR` | 第一个命中在 top-k 中的倒数排名——"第一个才对，排多前" |

**多模型对比示例**

```cmd
:: 模型A（不开 / 开筛选）
python scripts\eval_retrieval.py ... --model "模型A"     --out scripts\eval_results\eval_report.txt ...
python scripts\eval_retrieval.py ... --model "模型A-筛选" --use-filter --out scripts\eval_results\eval_report.txt ...

:: 模型B（相同查询集）
python scripts\eval_retrieval.py ... --model "模型B"     --out scripts\eval_results\eval_report.txt ...
python scripts\eval_retrieval.py ... --model "模型B-筛选" --use-filter --out scripts\eval_results\eval_report.txt ...

:: 想重开报告：删掉 scripts\eval_results\eval_report.txt 再跑
```

## 坑位速查

- **慢**：全量约 1550 条单发很慢，用 `--sample` / `--split` 只测一部分。
- **`--out` 是追加**：换模型跑都写同一个文件；要干净就删掉它。
- **embedding 模型找不到**：`--model-dir` 指向本机 `...\models\bge-small-zh-onnx`。
- **不要并行**：`--threads` 等并发手段已移除（BM25 吃 GIL，没用还慢）。