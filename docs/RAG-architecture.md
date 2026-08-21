# SafeRAG — RAG 架构与知识库摄取改动

> 本文随 `feat/legal-parser-refactor` 分支更新，描述当前代码的真实架构，并记录本次改动。
> 代码细节在 git 里，这里只记结构与决策。

---

## 全景：两条链路

### 索引侧（写入）

```
POST /api/v1/kb/files (上传 txt)
  → knowledge_service.upload_kb_file
     ① 写磁盘      → data/kb_source/{name}.txt          [文件本体]
     ② 解析登记    → SQLite kb_files 表 (building)      [权威源]
     ③ 切块入口    → legal_parser.build_chunks(text, source, file_type, tree_path)
                       ├─ looks_like_legal_txt(text)?
                       │   是 → parse_legal_txt → 文档树 → dump_tree_json(.tree.json) 侧车
                       │        → iter_legal_chunks  → 带 metadata 的结构化切块
                       │   否 → 退化 chunker.split_text → 纯文本切块（metadatas=None）
     ④ 索引        → kb_store.upsert_file_chunks
                       → BGE embedding → ChromaDB        [派生索引]
     ⑤ 登记 ready + reset_retriever()（缓存失效）
```

### 检索侧（读取）

```
document_service._process_document
  → _retrieve_context(事故文本)        [超长 query 自动分段]
    → get_retriever().retrieve(query, top_k=5)
          │ _ensure_loaded()  懒加载：ChromaDB 全量 + jieba 建 BM25
          ├─ _bm25_ids    jieba 分词 → BM25 打分        (全文)
          └─ _vector_ids  BGE embedding → ChromaDB 查询 (分段)
          → RRF 融合 (k=60) → top_k 条
  → 拼成带 [编号] 的 context → 进 prompt（命中带真实条号）
  → Qwen 生成报告 + 末尾「参考法规来源」附录
```

---

## 组件清单与职责

| 组件 | 文件 | 职责 |
|------|------|------|
| 法规解析器 | `legal_parser.py` | 判定是否法规 → 解析成文档树 → 结构化切块（带 metadata）。**本次新增 `build_chunks` 作为唯一切块入口** |
| 切块器（兜底） | `chunker.py` | 非法规文本的自适应切块：一行一条 vs 成段文章，句末标点二次切 |
| Embedding | `embedding_client.py` | BGE-small-zh ONNX int8，进程内加载，CLS pooling + L2 归一化 |
| 索引存储 | `kb_store.py` | ChromaDB 唯一入口，懒加载 client/collection 单例；`upsert_file_chunks` 接收 metadatas |
| 检索器 | `retriever.py` | 混合检索：BM25 + 向量 + RRF，进程级单例 |
| 登记册 | `kb_file_repo.py` | SQLite kb_files 表，文件级元数据 |
| 业务编排 | `knowledge_service.py` / `document_service.py` | 索引写入 / 检索调用 |
| 重建脚本 | `scripts/build_knowledge_base.py` | 索引损坏时从登记册重建；**本次也改走 `build_chunks`，与上传同源** |

---

## 权威关系（项目最核心的架构原则）

```
SQLite kb_files（权威源 Master）      ← 登记文件元数据，唯一事实
    ├── 磁盘 kb_source（文件本体）      ← 上传的原始 txt
    ├── 磁盘 *.tree.json（结构派生）    ← 解析出的文档树：章节条结构 + 正文
    └── ChromaDB（派生索引）           ← 只服务检索，可随时重建
          └── BM25（内存派生）          ← 从 ChromaDB 拉文档 jieba 构建
```

没有对账逻辑：写操作是"正向写两处/三处"，ChromaDB 或 tree.json 坏了都能从登记册 + 源文本用 `build_knowledge_base.py` 重建。

---

## 文档树（本次新增的结构化真相源）

法规文本不再让切块阶段"猜章/节/条"。`parse_legal_txt` 把规整的 txt 解析成结构化文档树，落盘成 `.tree.json` 侧车：

```json
{
  "doc": { "title": "中华人民共和国安全生产法", "file_type": "法律", "source": "安全生产法.txt", "meta": "（2021修正）" },
  "toc": [ {"level":"chapter","no":"第一章","title":"总则"}, ... ],
  "tree": [
    { "level": "chapter", "no": "第一章", "title": "总则", "children": [
        { "level": "section", "no": "第X节", "title": "...", "children": [
            { "level": "article", "no": "第一条", "title": "", "text": "为了加强安全生产…" },
            ...
        ]},
        { "level": "article", "no": "第X条", "title": "", "text": "…" },   // 直挂章下的条
    ]}
  ]
}
```

`iter_legal_chunks` 遍历这棵树按"条"切块，每块带完整 metadata：
`source / doc_title / file_type / chapter_no / chapter_title / section_no / section_title / article_no / article_chunk`。

**chunk 文本里也带层级前缀**，让向量检索能命中"法规名 + 章节"语义：

```
中华人民共和国安全生产法 第一章 总则
第一条 为了加强安全生产工作，防止和减少生产安全事故……
```

---

## 五个关键机制

- **懒加载 + 缓存失效**：`get_retriever()` 单例；上传/删除后 `reset_retriever()` 让下次重建
- **embedding 进程内**：onnx 模型常驻内存，ChromaDB 写入和查询共用同一个 embedding 函数
- **超长 query 分段**：>512 token 按 token 边界切成 ≤480 的段，分段向量检索合并
- **优雅降级**：检索任何异常 → `_retrieve_context` 返回空 → 纯 LLM 生成，不崩
- **引用溯源**：context 每条带 [编号]，报告末尾拼「参考法规来源」附录；本次改动让条号精确到"第X条"，追溯更强

---

## 本次改动内容（`feat/legal-parser-refactor`）

### 修的真问题（P0）

1. **目录探测写错** — `legal_parser` 原本是 `if line == "目录" or line == "目录"`，同一字面量写两遍且不去全角空格。法规里 `目　　录` 永远命中不了，正文靠"第一章重复出现"的 fallback 兜住才没炸。改：`_clean_line` 现归一全角空格（U+3000），单一 `"目录"` 比较。
2. **重复实现、漂移隐患** — `knowledge_service` 和 `build_knowledge_base` 各自手写一遍 `if looks_like_legal_txt → parse → iter_legal_chunks else split_text`。抽成 `legal_parser.build_chunks()`：四处逻辑（判法规/解析/切块/落树）只有一处实现，两边不再漂移。
3. **metadata schema 不一致** — 非法规块只有 `{source,chunk,md5}`，法规块带 `file_type/article_no…`，混在一个 collection 里 retriever 按 `file_type` 过滤会踩坑。现 `build_chunks` 对法规块统一产出固定 schema，`file_type` 一定带值，不再用空串兜底。

### 顺带清理（P1）

4. `iter_legal_chunks` 里 section/非section 两个几乎一样的 ~30 行分支合并成一个 `push_article()` 闭包，metadata 字段顺序和前缀拼装也统一。
5. `_clean_line` 现在去全角空格，`第一章　总　　则` 这类标题不再残留 `　　`。

### 文件变更

| 文件 | 变更 |
|------|------|
| `backend/core/legal_parser.py` | 新增；新增 `build_chunks` 公共入口；修目录探测；合并 `iter_legal_chunks` 重复分支；`_clean_line` 去全角空格 |
| `backend/services/knowledge_service.py` | 上传切块改调 `build_chunks`，移除内联解析逻辑和 `split_text` 依赖 |
| `scripts/build_knowledge_base.py` | 重建脚本同样改调 `build_chunks`，与上传同源 |
| `backend/core/kb_store.py` | `upsert_file_chunks` 接收 `metadatas` 参数（本次提交纳入的已有改动） |
| `tests/test_legal_parser.py` | 新增；新增"全角空格目录探测"、"build_chunks 分派 + metadata 一致性"用例 |

---

## 改造后的调用链

```
POST /api/v1/kb/files → upload_kb_file
  ① 写磁盘 kb_source/{name}.txt
  ② kb_file_repo.upsert (building)
  ③ legal_parser.build_chunks(text, source, file_type, tree_path)
       looks_like_legal_txt?
         是 → parse_legal_txt → 文档树 → dump_tree_json ({name}.tree.json)
              → iter_legal_chunks → (chunks, metadatas)        [带 chapter/article]
         否 → chunker.split_text → (chunks, None)                [纯文本]
  ④ asyncio.to_thread(kb_store.upsert_file_chunks, chunks, md5, metadatas)
       → 先删该文件旧块 → 分批 BGE embed → ChromaDB upsert
  ⑤ kb_file_repo.upsert (ready) + reset_retriever()
```

---

## 已知遗留（未在本次处理）

- **两套法规判定并存**：`chunker._looks_like_legal_text`（章/节≥2）和 `legal_parser.looks_like_legal_txt`（章/节/条≥3）。`split_text` 内部也会按章/条切法规，`build_chunks` 又在外层判一遍。理想是 `split_text` 只管非结构化兜底、法规判定唯一来源在 `legal_parser`，但这超出"修 bug"范围，未动。
- **`test_template_service.py` 失败**：模板更新提交（`403ea62`）后断言未同步，属预先存在的问题，与本次改动无关。
