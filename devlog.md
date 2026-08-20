# 开发日志

> 记做了什么、为什么、遇到什么问题。
> 代码细节在 git 里，这里只记决策和坑。

---

## 2026-08-20

- **多 worker + 引擎池**（feat/engine-pool 分支）：引擎进程内完全串行（`threading.Lock`），要并行只能多进程多端口
  - 决策：`QWEN_BASE_URLS` 逗号分隔引擎地址列表，`chat()` 轮询选引擎；`WORKER_COUNT` 控制并发 worker（默认 2）
  - 不做故障转移：引擎挂了任务失败就失败，保持简单
  - 关键点：每文档 = 一次 chat 调用 → 引擎在任务开始一次性选定，全程固定；轮询在 asyncio 单线程内无 await，原子安全
  - `_wake` 事件不用改：`Event.set()` 瞬间 resolve 所有 waiters，先醒的 clear 不会让后醒的错过
  - worker 循环加异常兜底：单 worker 崩溃 = 全停，N worker 崩溃 = 静默降容，不能让它死
  - 完整 prompt 的 INFO 日志降级为 DEBUG（多 worker 刷屏）
  - 风险未验证：BM1688 同 devid 多进程能否共用（`scripts/start_engines.sh` 已带回退）；2 worker 并发跑检索/embedding 是否线程安全

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
