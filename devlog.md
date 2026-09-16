# 开发日志

> 记做了什么、为什么、遇到什么问题。
> 代码细节在 git 里，这里只记决策和坑。

---

## 2026-09-15 · 模板系统重构 + 逐节顺序生成

- **需求（用户拍板四向）**：模板 = 数据库里的"有序节列表"（每节点式可自定标题+要求）；每用户看自己的，
  系统 3 套保留为示例；报告改为**逐节顺序生成**（后节引用前节、系统提示词大幅简略）；
  单一节可手动改、可追加"要求+材料"由 AI 只重生成该节。
- **模板模型**：`templates` 表 + sections JSON；**编号由索引派生**（一、二、三…）→ 删节自动重排，不存号；
  `owner_id=NULL`=系统示例，固定 id = 旧任务类型 key（accident_analysis…）→ 兼容旧 /tasks 和 task_type 调用。
- **逐节调度**：提交时对模板节做**快照**存 `doc.template_snapshot`（模板后改不影响已成文档）；
  worker 逐节生成、每节完成即落库（sections_json，前端可轮询进度）；
  `SECTION_CTX_BUDGET=2000` 截断前序（护 8K 输入窗口）；单节失败继续、最后按有无成功判终态。
- **单节精修**：`PATCH sections/{i}` 手动改；`POST sections/{i}/revise` 只重生成该节
  （重新检索法规 + 原文 + 本节 instruction + 本次补充 + 其它章节上下文），同步返回。
- **实现要点**：
  - 报告 .md 是下载/Word 唯一真相，sections_json 是逐节细粒度真相；统一走 report_builder.render_report。
  - 纯函数（消息拼接/截断/渲染）独立成 `report_builder.py`——不 import retriever/qwen，
    **本机无 jieba 也能单测**（CI"能测"与"不能测"的分界就在这）。
  - 旧文档（task_type 无快照）在 worker 兜底解析成系统模板的节，兼容运行。
- **契约**：[docs/template-system-api.md](docs/template-system-api.md)（给前端团队对接）。
- **坑/回归（修）**：`claim_next` 返回的内存对象是认领前 SELECT 的旧行（status 残留 queued）。
  逐节进度新增的中间 `update(doc)` 会把 DB 的 **processing 覆盖回 queued** → 前端处理期间
  一直显示"排队中"而非"处理中"。修：认领返回前把 `doc.status` 对齐 `processing`（DB 与内存一致）。
  旧代码整篇只在结尾 update 一次（此时 status 已是终态），所以从未暴露。
- **精修行为（用户拍板）**：精修改为**只基于本节自己内容 + 追加要求/材料**改进——不注入其它章节、
  不重新检索法规（防止 RAG 每次结果不同扰动局部改进）。代价：节间风格略脱节、无法规注入
  （要引条文就由前端贴进 `materials`）；`context` 参数保留，将来要加回 RAG 只改一行。
- 测试 45→51 passed（新增 template_service 归属/只读/idempotent + report_builder 纯函数）。

## 2026-09-15

- **分节树泛化（非法规文本也分节）**：[legal_parser.py](backend/core/legal_parser.py) 新增 `_parse_plain`，
  取代旧的"非法规整段塞单 article"。
- 决策：
  - 有结构标题（中文序号 `一、` / 括号序号 `（一）` / 阿拉伯 `1.` / 多级 `1.1` / markdown `#` / 第X章·节）
    → 建成 chapter/section/article 层级树，chunk 带章节元数据；
  - 无结构 → 按空行/长度分块（max_chars 400 收口），不再一把整块怼进 embedding。
- 关键边界：`iter_legal_chunks` 对"顶层裸 article"与"chapter→section/article→article"两种树形本就兼容，
  **所以只改解析侧、入库/检索链路零改动**。标题检测顺序敏感：多级阿拉伯必须先进 `_AR_LEVEL_RE`，
  否则 `1.1.2` 会被 `1.` 前缀吃掉。
- 坑/护栏：标题检测最容易误伤"长得像编号的完句长句"（如 `1. 针对上述问题，应当……。`）
  ——护栏 = 清洗后 ≤50 字 + 不以句读（。！？；）结尾；markdown 标题必须在**原始行**匹配
  （`_clean_line` 会吞掉 `#` 后的空格）。
- 顺手补测：`_split_long_text` 分段逻辑此前**零覆盖**（现有用例文本都短、切分分支从未被触发），
  本次补边界切分 / 超长单句硬切（保住 embedding 512 token 上限）/ 无损拼接三条。
- 结果：测试 35→44 passed，现有全部用例回归通过。

## 2026-09-14

- **删除 deb 部署（非 Docker 路径）**：用户拍板"现在不用这个部署"。删的是整套非 Docker 部署：
  `scripts/deploy/make_deb.sh` / `scripts/deploy/debian/`（两个 deb 的 DEBIAN 打包文件）/
  `scripts/deploy/systemd/`（qwen / qwen_chat / saferag 三服务）/ 宿主 `scripts/deploy/nginx/` /
  `scripts/deploy/README.md`（deb 安装指南）。
- 边界判断：这条路径的所有组件只被 deb 打包消费（conffiles/postinst/prerm 引用 systemd 三服务，
  make_deb 打包宿主 nginx 与前端）；当前在跑的 Docker 部署完全自足不碰它们——删了不破坏现网。
- 同步改：release.yml 去掉 make_deb 步骤、CI-PLAN 去掉 deb 引用、.gitignore 过期注释、
  docker nginx 注释里的悬空引用。后果：Qwen3_5 在仓库里的消费方只剩镜像构建机，拆独立引擎仓库任务简化。
- 坑/教训：删"一个入口"前先查它被谁消费——deb 摸出来的事实是它=整套 systemd+nginx 非 Docker 路径的
  打包入口。只删 make_deb.sh 会留下 3 个无人用的 systemd unit 和十几份 nginx 配置（死文件）。
  好在 docker 侧自己的 nginx 配置（scripts/deploy/docker/nginx/saferag.conf）与这套宿主 nginx 独立。

## 2026-09-02

- **三权分立认证 + 操作日志审计**：sysadmin/secadmin/audadmin 三员，登录成败、报告、知识库（含敏感标记）、用户管理的写操作全部落 `operation_log`，仅 audadmin 可查（`audadmin_only`）
- 决策：授权以 DB 角色为准，不看 JWT 里的 role 声明——停用/改角色立即生效
- 决策：审计写入失败绝不上抛（`record` 内部吞异常只记 logger），审计把业务打翻是生产事故

- **权限矩阵重排**：新增普通用户 `user` 角色，依赖按功能命名（business_write / user_admin / monitor_view / kb_upload / kb_delete）
- 坑：audit-log 合进 sanyuan 后重构删了 `audadmin_only`，audit 路由 ImportError 应用起不来——按 sysadmin_only 同款别名补回才恢复
- 教训：改权限名后必须全局搜 `from ...auth_service import` 核对残留引用，别等 import 报错

- **health.py 启动即崩**：`Depends(any_role)` 用了未导入的符号，模块 import 时 NameError，整个应用起不来
- 教训：验证应用能启动，先 `python -c "import backend.main"` 冒烟，别等运行时

- **误提交 + 分支漂移**：操作日志被误提交到 sanyuan，用 临时分支 feat/audit-log + reset 挪走再合并回来；多会话并行时分支会来回跳
- 教训：改文件前先 `git branch --show-current` 确认自己在哪，避免改错分支

- **环境坑**：requirements 锁了 PyJWT/jieba，但本机 Anaconda 只装了 PyJWT 缺 jieba，整包启动无法在本地验证
- 教训：锁版本 ≠ 本机已装，提交流前先在目标环境 `pip install -r backend/requirements.txt`

## 2026-08-12

- **SQLite 替代纯文件存储**：元数据和原文进 SQLite，报告 .md 留在文件系统（方便下载）
- 原因：文件遍历读 meta.json 做不了列表查询，前端以后需要列表页
- 新增 `database.py`，重写 `document_repo.py`（函数签名不变，上层无感知），新增 `list_all()` / `count()`
- Python 自带 sqlite3，ARM 盒子零依赖

- **所有路由加 /api/v1/ 前缀**：为以后 v2 留空间，前端这次需要同步改调用地址

- **限制 max_length=1000**：Qwen3.5 bmodel MAX_INPUT_LENGTH=1024，超了流式模式下直接断连无报错，只能客户端挡

- **check_health 超时从 5s 改为 3s**：TPU 空闲时 100ms 内回应，3s 足够判断

- **字段校验**：POST /documents/process 加 Pydantic Field 校验（min_length / max_length）

- **config.py**：集中管理所有配置，支持环境变量覆盖

## 2026-08-11

- **.so 文件被误 gitignore**：我 blanket 忽略了 *.so，用户指出它们是编译好的 TPU 推理引擎，必须跟踪。从 .gitignore 里移除 *.so
- 教训：不要盲目加通配符 ignore，先搞清楚文件是什么

- **后端三层架构搭建**：api → service → repository，文件系统存储，对接 Qwen 推理引擎
- 第一次搞太复杂（抽象接口、依赖注入、全局异常处理），被用户推翻重来
- 教训：先简后繁，单文件 repo 函数够用就不要上抽象

## 2026-08-11（未提交到 git 的调研）

- **Qwen3.5-4B TPS 只有 7 而非预期的 24**：花大量时间排查
- 根因：官方 BM1688 的 Qwen3.5-4B bmodel 是多模态版本（3+ GB, Num Layers:32），网上 24 token/s 是纯对话版（2.3GB），模型不一样
- Driver 0.4.13 可能偏旧，TPU 温度 42°C、时钟 900/1000 MHz，硬件正常
- 结论：放弃 Qwen3.5，计划换 Qwen3-4B

- **server.py / pipeline_text.py 不是官方文件**：我以为是 Sophgo 官方 demo，用户纠正——这是 Qwen3_5 目录自带的项目文件，换模型需要对应目录自带的新版本
