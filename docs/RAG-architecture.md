# SafeRAG — RAG 架构与知识库摄取改动

> 本文随功能分支更新，描述当前代码真实架构。
> 代码细节在 git 里，这里只记结构与决策。

---

## 全景：两条链路

### 索引侧（写入）—— 解析与入库两段式，文档树是活合同

```
POST /api/v1/kb/files (上传文件: file_type/region/city 表单字段)
  → knowledge_service.upload_kb_file(filename, content, file_type, region, city)
     （格式无关，校验后缀白名单 + 大小；region=省/city=地级市，都留空=全国性文件）
     ① 写磁盘      → data/kb_source/{name}          [文件本体]
     ② 解析登记    → SQLite kb_files 表 (building)  [文件级权威源]

     ③ 解析段（bytes → 文档树，合同确立，按后缀分派）
        tree, md5 = legal_parser.parse_to_tree(content: bytes, filename, file_type, region, city="")
        内部 _extract_text(content, filename)：
            .txt  → decode_text 自动兼容 utf-8/gbk/gb18030
            .docx/.pdf → 暂 NotImplementedError（加提取器即纯加法）
        → 法规（章/节/条头足够） / 非法规（最简树单根 article）
        → kb_tree_repo.save(filename, tree, md5)
          落盘 SQLite kb_trees 表                  [结构真相源 / 活合同]

     ④ 入库段（只认树，从不重新解析）
        chunks, metadatas = iter_legal_chunks(tree) [遍历树按条出块，带 metadata]
        → kb_store.upsert_file_chunks → BGE embedding → ChromaDB  [派生索引]
     ⑤ 登记 ready + reset_retriever()（缓存失效）

        ★ 新文件格式只动 _extract_text 一行 + 放开 ALLOWED_EXTS，入库段零改动 ★
```

### 检索侧（读取）—— 三级地域筛选 + 可选精排

```
document_service.retrieve_with_citations(事故文本, top_k=5, provinces, cities, file_types)
  → get_retriever().retrieve(query, top_k=5, provinces, cities, file_types)
        │ _ensure_loaded()  懒加载：ChromaDB 全量 + jieba 建 BM25
        ├─ _candidate_ids   三级筛选缩候选集（文件类型 → 省 → 市，打分前过滤）
        ├─ _bm25_ids    jieba 分词 → BM25 打分        (候选集内)
        └─ _vector_ids  BGE embedding → ChromaDB 查询  (候选集内，超长 query 分段)
        → RRF 融合 (k=60) → 粗取 RERANKER_TOP_N 条（默认 20）
        → [可选] reranker 精排（bge-reranker cross-encoder）→ 截 top_k=5 条
  → 拼成带 [编号] 的 context → 进 prompt（命中带真实条号）
  → Qwen 生成报告 + 末尾「参考法规来源」附录
```

---

## 组件清单与职责

| 组件 | 文件 | 职责 |
|------|------|------|
| 文档解析器 | `legal_parser.py` | `parse_to_tree(content: bytes, filename, file_type, region, city="") -> (tree, md5)`：按后缀分派 bytes→文本→文档树（法规层级树 / 非法规最简树），region/city 进 doc+chunk。`iter_legal_chunks`：从树遍历出 chunks+metadata，地域拼进文本前缀。新格式在 `_extract_text` 加一行即接上 |
| 文档树仓库 | `kb_tree_repo.py` | 文档树的 SQLite 权威落盘（save/load/delete）。解析与入库间的活合同 |
| 编码/指纹工具 | `chunker.py` | `decode_text`/`read_text`/`file_md5`——底层的文本读取与内容指纹。切块逻辑已迁入 legal_parser |
| Embedding | `embedding_client.py` | BGE-small-zh ONNX int8，进程内加载，CLS pooling + L2 归一化 |
| 精排器 | `reranker.py` | **可选** cross-encoder 精排（bge-reranker）。模型文件就位才启用；加载/推理异常降级为原顺序，绝不拖垮检索 |
| 索引存储 | `kb_store.py` | ChromaDB 唯一入口，懒加载 client/collection 单例；`upsert_file_chunks` 接收 metadatas |
| 检索器 | `retriever.py` | 混合检索：BM25 + 向量 + RRF，进程级单例。三级地域筛选（文件类型→省→市）+ 可选 reranker 精排 |
| 登记册 | `kb_file_repo.py` | SQLite kb_files 表，文件级元数据（md5/file_type/region/city/status…） |
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
`source / doc_title / file_type / region / city / chapter_no / chapter_title / article_no / article_chunk`（有节再加 `section_no/section_title`）。
chunk 正文前缀会拼上 region/city（有则拼），让 embedding 向量与 BM25 都"认识"文件的地域属性。

**活合同的意义**：解析段把树存进 SQLite，入库段只 `load` 树再切块，两边解耦。未来"外部预处理"只需在某处产出 JSON → 调 `kb_tree_repo.save`，入库一段即可，不必碰解析。

---

## 三级地域筛选（文件类型 → 省 → 市）

用户可多选**文件类型 / 省 / 市**（前端级联下拉），检索在打分前先缩候选集
（`Retriever._candidate_ids`），从根上排除"上海事故查到湖北条例"。

| 勾选 | 检索范围 |
|---|---|
| 都不勾 | 全库（旧语义） |
| 只勾文件类型（如"国家法律"） | 仅该类型 |
| 勾"地方法规"（省/市空） | 全部地方法规 |
| 勾省（如"湖北"，市空） | **湖北省级**条例（city 空） |
| 省+市同选（湖北 + 武汉） | 省级条例（跟省出现）+ **武汉市**条例 |
| 只勾市（武汉，省空） | 仅武汉市条例——市级**不受省约束**，勾哪个市就要哪个市的法 |
| `file_types=[]`（显式空） | 什么都不检索 |

规则要点：

- **国家法律恒在**——仅在"未做文件类型筛选"或"类型含国家法律"时保留，省/市字段留空。
- 省市粒度按 **省（直辖市/自治区）+ 地级市**：省级条例 city 留空，市级条例 city=市名。
  **City 以地级市为准**，入库时前端传入，后端不判断粒度。
- 地域会拼进 chunk 文本前缀与 metadata（`region`/`city`），embedding/BM25 也"认识"地域属性。
- 接口统一收 `provinces: list[str] / cities: list[str] / file_types: list[str]`；文档库用逗号分隔字符串
  存储，处理时切回 list（空串 → `[]`=显式空，区别于未传 `None`=未做筛选）。

## reranker 精排（可选层）

BM25+向量+RRF 是粗排；`reranker.py` 是可选精排层（bge-reranker cross-encoder）：把粗取出的候选池
逐条与 query 组 token 对打分，相关性比点积更准。

- **启用条件**：`RERANKER_MODEL_PATH` 配置且模型文件就位（`is_available()`）；粗取池 `RERANKER_TOP_N`（默认 20）。
- 精排后截 `top_k=5` 返回，`score` 换成精排 logit。
- **完全可选、降级安全**：模型缺失 → 静默走无精排；运行时加载/推理抛错 → 原顺序返回，检索不中断。
- 评估脚本 `scripts/eval_retrieval.py` 支持 `--rerank-pool` / `--rerank-model` / `--no-rerank` 横向对比精排效果。

---

## 关键机制

- **懒加载 + 缓存失效**：`get_retriever()` 单例；上传/删除后 `reset_retriever()` 让下次重建
- **三级地域筛选**：文件类型→省→市 打分前缩候选集（见上节），多选可叠加
- **reranker 精排**：可选分层，模型缺失即降级（见上节）
- **embedding 进程内**：onnx 模型常驻内存，ChromaDB 写入和查询共用同一个 embedding 函数
- **超长 query 分段**：>512 token 按 token 边界切成 ≤480 的段，分段向量检索合并
- **优雅降级**：检索任何异常 → `retrieve_with_citations` 降级为不注入法规 → 纯 LLM 生成，不崩
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
- **`.docx`/`.pdf` 未实现**：`parse_to_tree` 已按后缀分派、`_extract_text` 占位抛 `NotImplementedError`，service 白名单 `ALLOWED_EXTS` 现只含 `.txt`。真要支持时：①在 `_extract_text` 加提取器分支（如 python-docx/pypdf，需装依赖）；②把后缀加进 `ALLOWED_EXTS`；③`build_knowledge_base._md5_of` 对齐多格式 md5 语义。入库段不动。
- **`test_template_service.py` 失败**：模板更新提交后断言未同步，属预先存在的问题，与本改动无关。

---

### 第三批：接缝——parse_to_tree 吃 bytes，多格式纯加法

第二批让 `parse_to_tree` 吃 `text: str`，service 硬绑 `.txt` + `decode_text`，加 docx/pdf 会被挡死。本批把接缝接好：

- `parse_to_tree(content: bytes, filename, file_type) -> (tree, md5)`：按 `filename` 后缀在 `_extract_text` 里 bytes→文本，再走向树。md5 基于提取文本算（语义沿用，存量无迁移）。
- service 删 `decode_text`/`.txt` 校验，改后缀白名单 `ALLOWED_EXTS`（现仅 `.txt`），解构 `(tree, md5)`。
- `build_knowledge_base.py` 读 bytes 调 `parse_to_tree`，保留轻量 md5 预判跳过。
- md5 由解析段返回 → service/build 都解构；入库段零改动。
- 测试输入改 bytes，加"未支持后缀抛 `NotImplementedError`"用例。

接缝接好后，新格式是纯加法：`_extract_text` 加一行 + `ALLOWED_EXTS` 放一个后缀，入库链路不动。

---

### 第四批：三级地域筛选（省+市+文件类型） + reranker 精排

#### 决策

1. **复用 `region` 当省，新增 `city` 字段（地级市）**。kb_files 表、文档树 doc、chunk metadata 同步扩展；
   存量数据 region 语义不变，city 默认空（存量省级条例 city 空即正确）。
2. **检索三级联动语义**（用户拍板）：国家法恒在；只勾省 → 仅该省省级条例；省+市同选 →
   省级条例跟省出现 + 市级条例；只勾市 → 市级条例不受省约束；`file_types=[]`（显式空）→ 什么都不检索。
3. **地域拼进 chunk 文本前缀**：让 embedding/BM25 都"认识"文件的地域属性，市级过滤时向量也能感知。
4. **接口收 `list`**：检索/文档生成/聊天接口统一 `provinces/cities/file_types` 三个 `list[str]`，
   旧的单值 `region` 参数保留作废弃兼容。
5. **reranker 做成可选层**：模型文件就位才启用（`is_available()`），缺失/异常静默降级，不动检索主链路。

#### 改动

| 文件 | 变更 |
|------|------|
| `backend/core/database.py` | kb_files 表加 `city` 列（幂等 ALTER） |
| `backend/core/legal_parser.py` | `parse_to_tree` 加 `city` 参数；doc/chunk 带 region+city；chunk 前缀拼地域 |
| `backend/core/retriever.py` | `_candidate_ids(provinces, cities, file_types)` 三级缩候选；`retrieve` 透传；接入可选 reranker 精排 |
| `backend/core/reranker.py` | **新增**。bge-reranker cross-encoder 精排客户端：`is_available()` / `rerank()`，异常降级 |
| `backend/repositories/kb_file_repo.py` | upsert 带 city；`list_files` 支持 city 过滤 |
| `backend/repositories/document_repo.py` | Document 持久化 provinces/cities/file_types；get/list 改用 `_row_to_doc` 防漏读（修重试冲空历史 bug） |
| `backend/services/knowledge_service.py` | 上传/解析/入库透传 region+city；失败分支 upsert 也带（防 ON CONFLICT 冲空） |
| `backend/services/document_service.py` | `retrieve_with_citations` / `create_document` 透传筛选三列 |
| `backend/services/chat_service.py` | 聊天 RAG 检索透传筛选三列 |
| `backend/api/kb.py` | 上传表单/列表支持 city；列表项含 city |
| `backend/api/documents.py` / `chat.py` | 请求体新增 `provinces/cities/file_types` list（旧 `region` 保留废弃） |
| `scripts/eval_retrieval.py` | 评估支持 `--use-filter`（地区+类型筛选）、`--rerank-pool/--rerank-model/--no-rerank`、`--sample` 抽样 |
| `scripts/build_knowledge_base.py` | 重建携带 region/city（成功/失败 upsert 都带，防冲空） |

#### 风险与语义约定

- **City 粒度**：入库按**地级市**（前端传，后端不判断粒度）。若混入县级市/区名（如"惠东" vs "惠州"），
  勾"惠州"匹配不到"惠东"的文件——依赖前端级联传值与文件名一致。
- **多选性能**：候选过滤仍是遍历 metadata 的既有机制，量级不变，无额外代价。
- **region/city 拼前缀**：多地域选中时 chunk 前缀带省/市名，块数可能微增（max_chars 400 截断）。
- **reranker 本机开、TPU 盒子上静默关**：config 默认 `RERANKER_MODEL_PATH` 是本机 D: 路径，盒子无该文件
  即降级为无精排——盒子上跑评估必须显式 `--rerank-model /data2/models/bge-reranker-base`。
