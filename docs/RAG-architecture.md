基于当前代码，给你画一张完整的 RAG 架构图。

全景：两条链路

┌─────────────────────── 索引侧（写入） ───────────────────────┐
│ POST /api/v1/files (上传 txt)                                │
│   → knowledge_service.upload_kb_file                        │
│      ① 写磁盘    → data/kb_source/{name}.txt       [文件本体] │
│      ② 登记册    → SQLite kb_files 表 (building)    [权威源]   │
│      ③ 切块      → chunker.split_text (200字符/块)           │
│      ④ 索引      → kb_store.upsert_file_chunks              │
│                      → BGE embedding → ChromaDB   [派生索引]  │
│      ⑤ 登记 ready + reset_retriever() (缓存失效)              │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────── 检索侧（读取） ───────────────────────┐
│ document_service._process_document                          │
│   → _retrieve_context(事故文本)      [超长 query 自动分段]     │
│     → get_retriever().retrieve(query, top_k=5)              │
│         │ _ensure_loaded()  懒加载：ChromaDB 全量 + jieba 建 BM25│
│         ├─ _bm25_ids    jieba 分词 → BM25 打分    (全文)      │
│         └─ _vector_ids  BGE embedding → ChromaDB 查询 (分段)  │
│         → RRF 融合 (k=60) → top_k 条                          │
│   → 拼成带 [编号] 的 context → 进 prompt                       │
│   → Qwen 生成报告 + 末尾「参考法规来源」附录                    │
└─────────────────────────────────────────────────────────────┘
组件清单与职责
组件	文件	职责
切块器	chunker.py	自适应切块：一行一条 vs 成段文章，200 字符上限，按句末标点二次切
Embedding	embedding_client.py	BGE-small-zh ONNX int8，进程内加载，CLS pooling + L2 归一化
索引存储	kb_store.py	ChromaDB 唯一入口，懒加载 client/collection 单例
检索器	retriever.py	混合检索：BM25 + 向量 + RRF，进程级单例
登记册	kb_file_repo.py	SQLite kb_files 表，文件级元数据
业务编排	knowledge_service.py / document_service.py	索引写入 / 检索调用
权威关系（你项目最核心的架构原则）

SQLite kb_files（权威源 Master）      ← 登记文件元数据，唯一事实
    ├── 磁盘 kb_source（文件本体）      ← 上传的原始 txt
    └── ChromaDB（派生索引）           ← 只服务检索，可随时重建
          └── BM25（内存派生）          ← 从 ChromaDB 拉文档 jieba 构建
没有对账逻辑：写操作是"正向写两处"，ChromaDB 坏了用 build_knowledge_base.py 从登记册重建。

检索器的内部结构
Retriever 单例懒加载（retriever.py:37）：


首次检索时 _ensure_loaded()：
  拉 ChromaDB 全部 documents + metadatas
  → 每块 jieba 分词 → BM25Okapi 索引（全量在内存）

每次 retrieve()：
  BM25 路   → jieba.lcut(query) → BM25 打分全库 → top_n
  向量路    → BGE embedding(query) → ChromaDB.query → top_n   [超长分段]
  RRF 融合  → score = Σ 1/(k+rank)，k=60 → 取 top_k
五个关键机制
懒加载 + 缓存失效：get_retriever() 单例；上传/删除后 reset_retriever() 让下次重建
embedding 进程内：onnx 模型常驻内存，ChromaDB 写入和查询共用同一个 embedding 函数
超长 query 分段（刚改的）：>512 token 按 token 边界切成 ≤480 的段，分段向量检索合并
优雅降级：检索任何异常 → _retrieve_context 返回空 → 纯 LLM 生成，不崩
引用溯源：context 每条带 [编号]，报告末尾拼「参考法规来源」附录，可追溯