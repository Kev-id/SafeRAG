# SafeRAG — RAG 架构与知识库摄取改动

> 本文随 `feat/legal-parser-refactor` 分支更新，描述当前代码真实架构。
> 代码细节在 git 里，这里只记结构与决策。

---

## 全景：两条链路

### 索引侧（写入）—— 解析与入库两段式，文档树是活合同

```
POST /api/v1/kb/files (上传 txt)
  → knowledge_service.upload_kb_file
     ① 写磁盘      → data/kb_source/{name}.txt          [文件本体]
     ② 解析登记    → SQLite kb_files 表 (building)      [文件级权威源]

     ③ 解析段（文本 → 文档树，合同确立）
        text → legal_parser.parse_to_tree(text, source, file_type)
                 法规（章/节/条头足够）  → 章/节/条 层级树
                 其它文本               → 最简树（单根 article 装整段正文）
              → kb_tree_repo.save(filename, tree, md5)
                 落盘 SQLite kb_trees 表                [结构真相源 / 活合同]

     ④ 入库段（只认树，从不重新解析）
        tree = kb_tree_repo.load(filename)
        chunks, metadatas = iter_legal_chunks(tree)     [遍历树按条出块，带 metadata]
        → kb_store.upsert_file_chunks → BGE embedding → ChromaDB  [派生索引]
     ⑤ 登记 ready + reset_retriever()（缓存失效）
```

### 检索侧（读取）—— 不变

```
document_service._process_document
  → _retrieve_context(事故文本)        [超长 query 自动分段]
    → get_retriever().retrieve(query, top_k=5)
          │ _ensure_loaded()  懒加载：ChromaDB 全量 + jieba 建 BM25
          ├─ _bm25_ids    jieba 分词 → BM25 打打分        (全文)
          └─ _vector_ids  BGE embedding → ChromaDB 查询 (分段)
          → RRF 融合 (k=60) → top_k 条
  → 拼成带 [编号] 的 context → 进 prompt（命中带真实条号）
  → Qwen 生成报告 + 末尾「参考法规来源」附录
```

---

## 组件清单与职责

| 组件 | 文件 | 职责 |
|------|------|------|
| 法规解析器 | `legal_parser.py` | `parse_to_tree`：任意文本→文档树（法规层级树 / 非法规最简树）。`iter_legal_chunks`：从树遍历出 chunks+metadata |
| 文档树仓库 | `kb_tree_repo.py` | 文档树的 SQLite 权威落盘（save/load/delete）。解析与入库间的活合同 |
| 编码/指纹工具 | `chunker.py` | `decode_text`/`read_text`/`file_md5`——底层的文本读取与内容指纹。切块逻辑已迁入 legal_parser |
| Embedding | `embedding_client.py` | BGE-small-zh ONNX int8，进程内加载，CLS pooling + L2 归一化 |
| 索引存储 | `kb_store.py` | ChromaDB 唯一入口，懒加载 client/collection 单例；`upsert_file_chunks` 接收 metadatas |
| 检索器 | `retriever.py` | 混合检索：BM25 + 向量 + RRF，进程级单例 |
| 登记册 | `kb_file_repo.py` | SQLite kb_files 表，文件级元数据（md5/file_type/status…） |
| 重建脚本 | `scripts/build_knowledge_base.py` | 索引损坏时从登记册重建；现解析成树存 kb_trees，再从树出块入库 |

---

## 权威关系（项目最核心的架构原则）

```
SQLite kb_files（文件级权威源）      ← 登记文件元数据，唯一事实
    ├── 磁盘 kb_source（文件本体）    ← 上传的原始 txt
    └── ChromaDB（派生索引）         ← 只服务检索，可随时重建
          └── BM25（内存派生）        ← 从 ChromaDB 拉文档 jieba 构建

SQLite kb_trees（结构真相源）        ← 文档树：章/节/条结构 + 正文。解析产出、入库读取的活合同
                                     ← 与 kb_files 同键(filename)，删文件联动删
```

写操作"正向写"：上传=写磁盘+kb_files+kb_trees+Chroma；删除=Chroma+磁盘+kb_files+kb_trees。
没有对账逻辑： ChromaDB 坏了用 `build_knowledge_base.py` 重建（树也会随之重建）；kb_trees 坏了从源文本重解析即可。

---

## 文档树（解析与入库的活合同）

不再让入库阶段去猜章/节/条。`parse_to_tree` 把任意文本解析成统一 schema 的文档树，落 `kb_trees` 表：

```json
{
  "doc": { "title": "中华人民共和国安全生产法", "file_type": "法律", "source": "安全生产法.txt", "meta": "（2021修正）" },
  "toc": [ {"level":"chapter","no":"第一章","title":"总则"}, ... ],
  "tree": [
    { "level": "chapter", "no": "第一章", "title": "总则", "children": [
        { "level": "article", "no": "第一条", "title": "", "text": "为了加强安全生产…" }
    ]}
  ]
}
```

非法规文本则是最简树（同级可直接挂裸 article）：

```json
{ "doc": {"title":"","file_type":"说明","source":"notes.txt","meta":null},
  "toc": [],
  "tree": [{"level":"article","no":"","title":"","text":"整段正文…"}] }
```

`iter_legal_chunks` 遍历树顶层或章节下的 article，超长条文按 max_chars 切多块，每块带固定 schema 的 metadata：
`source / doc_title / file_type / chapter_no / chapter_title / article_no / article_chunk`（有节再加 `section_no/section_title`）。

**活合同的意义**：解析段把树存进 SQLite，入库段只 `load` 树再切块，两边解耦。未来"外部预处理"只需在某处产出 JSON → 调 `kb_tree_repo.save`，入库一段即可，不必碰解析。

---

## 五个关键机制

- **懒加载 + 缓存失效**：`get_retriever()` 单例；上传/删除后 `reset_retriever()` 让下次重建
- **embedding 进程内**：onnx 模型常驻内存，ChromaDB 写入和查询共用同一个 embedding 函数
- **超长 query 分段**：>512 token 按 token 边界切成 ≤480 的段，分段向量检索合并
- **优雅降级**：检索任何异常 → `_retrieve_context` 返回空 → 纯 LLM 生成，不崩
- **引用溯源**：context 每条带 [编号]，报告末尾拼「参考法规来源」附录；条号精确到"第X条"，追溯更强

---

## 本次改动内容

### 第一批（`feat/legal-parser-refactor` 早期提交）

抽 `legal_parser.build_chunks` 公共入口、修目录探测 bug、合并 `iter_legal_chunks` 重复分支、`_clean_line` 去全角空格、统一 metadata schema。**详见该批提交**。

### 第二批：解析与入库解耦，tree 变活合同（本次）

#### 决策

1. JSON 文档树做成**中间契约**——解析产树存 SQLite，入库从 SQLite 读树，不再写完就丢。
2. **不兼容旧 txt 兜底**——`build_chunks`/`dump_tree_json`/`split_text` 全删。
3. **非法规文本包成最简文档树**，与法规走同一条入库路径——入库逻辑只有一条。
4. 文档树存 **SQLite `kb_trees` 表**，不存磁盘侧车。

#### 改动

| 文件 | 变更 |
|------|------|
| `backend/core/legal_parser.py` | `parse_legal_txt` → `parse_to_tree`（非法规产出最简树）；删 `build_chunks`/`dump_tree_json`/`looks_like_legal_txt`/`_fallback_split`；`iter_legal_chunks` 顶层支持裸 article；清无用 import |
| `backend/repositories/kb_tree_repo.py` | **新增**。文档树 SQLite 仓库：save/load/delete |
| `backend/core/database.py` | `init_db` 建 `kb_trees` 表 |
| `backend/services/knowledge_service.py` | `upload_kb_file` 改两段式（解析存树 / 入库从树读）；删除时联动删 `kb_trees` |
| `scripts/build_knowledge_base.py` | 重建现解析存 kb_trees，再从树出块入库；删 `split_text` 依赖 |
| `backend/core/chunker.py` | 删 `split_text` 及全部切块 helper，只留 `decode_text/read_text/file_md5` |
| `tests/test_legal_parser.py` | 改测 `parse_to_tree` + `iter_legal_chunks`；新增非法规最简树、`kb_tree_repo` 往返忠实性用例；删依赖 `build_chunks` 的用例 |
| `tests/test_chunker.py` | 删除（测已移除的 `split_text`） |

#### 风险与遗留

- **非法规整段入单块**：超长普通文本不切分，单 chunk 可能很大、检索质量下降。本批知识库定位=法规，接受；未来若有长篇普通文，在 `parse_to_tree` 的非法规分支里加分段即可。
- **存量 `.tree.json` 侧车**：旧部署或留有磁盘侧车文件，入库改从 SQLite 后成死文件，可手动删。
- **`test_template_service.py` 失败**：模板更新提交后断言未同步，属预先存在的问题，与本改动无关。
