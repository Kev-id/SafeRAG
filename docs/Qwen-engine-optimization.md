# Qwen 推理链路优化说明

> 背景：推理引擎（Qwen3.5-4B，8196 上下文，TPU 单卡）推理耗时长，导致后端出现超时、健康检查失败、请求互相阻塞等问题。本文记录本次优化的原理与改动内容。

---

## 一、遇到的问题（症状）

1. **文档处理经常超时**：`httpx.ReadTimeout`，文档被置为 failed。
2. **ai_status 经常查不到引擎**：显示"连不上/正在处理其它请求"，但引擎其实在正常跑。
3. **错误信息误导**：日志报 `无法连接 Qwen 推理引擎: `（冒号后是空的），实际是**超时**而不是连不上。
4. 推理期间，其他请求（如 `/health`）也被卡住。

## 二、根因分析

### 根因 1（最关键）：非流式接口阻塞了事件循环

推理引擎的 `chat_completions` 是 async 端点，但内部**同步**调用 `run_chat`：

```python
@app.post("/v1/chat/completions")
async def chat_completions(req):
    text = run_chat(req.messages)   # 同步、CPU 密集，整个推理在这里跑完
```

`run_chat` 没有任何 `await`，在 async 端点里直接同步执行 → **整个推理期间，FastAPI 的事件循环被占死**：

- `/health` 排不上队 → ai_status 一查就超时
- 第二个推理请求也被卡住，等待时间叠加到推理时间上，更容易超过客户端超时

### 根因 2：超时是单一固定值，两端都不合理

`QWEN_TIMEOUT=300` 一个值套在所有阶段：
- **连接超时** 300s：局域网内本该 <1s，设为 300s 等于没有超时保护。
- **读超时** 300s：8K 模型长推理经常超过，导致 ReadTimeout。

### 根因 3：每次调用新建 AsyncClient

`chat()` / `check_health()` 每次调用都 `httpx.AsyncClient(...)`，反复重建连接池，浪费且增加延迟。

### 根因 4：max_tokens 形同虚设

请求 schema 里有 `max_tokens` 字段，但生成循环**从未使用它**——一直生成到 `im_end` 或塞满 SEQLEN 才停，最坏情况生成接近 8K token。

### 根因 5：ai_status 拿不到真实状态

`/health` 只返回 `{"status": "ok"}`，不反映引擎是否在忙；客户端 `check_health` 只判断 HTTP 200，无法区分"在线 / 推理中 / 连不上"。

---

## 三、核心原理

### 1. 事件循环与同步阻塞（asyncio）

FastAPI 单进程只有一个事件循环，所有请求的协程在它上面轮流执行。**一个协程里如果有同步 CPU 密集代码，其他协程全部阻塞**。所以 CPU 密集的推理必须丢出事件循环：

```python
text = await asyncio.to_thread(run_chat, req.messages, req.max_tokens)
```

`asyncio.to_thread` 把 `run_chat` 丢进线程池执行，`await` 挂起当前协程但不占事件循环——推理期间 `/health` 等其他请求照常处理。`run_chat` 内部已有 `threading.Lock` 保证同一时刻只有一个推理在跑（单 TPU 串行），放线程池并发安全。

### 2. 连接池复用

一个进程里多次 HTTP 调用（`chat`、`check_health`）应该共用一个 `AsyncClient`，复用 TCP 连接，避免每次重建连接池。单例懒加载，进程退出自然回收。

### 3. 超时拆分

连接超时（建立 TCP/HTTP 连接）应该短（局域网 5s 足够），读超时（等响应/推理完成）应该长（8K 模型按实际耗时定）。httpx 支持 `httpx.Timeout(connect=5, read=600)` 分别设置。

### 4. max_tokens 封顶生成

生成循环加 `tok_num < max_tokens` 条件，配合客户端发送 `max_tokens`，把单次生成的最坏耗时封顶。`max_tokens` 传 None 时保持不限（兼容旧调用）。

### 5. 状态探针（busy 标志）

`/health` 返回 `busy`（模型锁是否被持有）。客户端解析后，ai_status 能区分三种真实状态：**在线 / 推理中 / 连不上**。

---

## 四、改动清单

### 推理引擎（`Qwen3_5/python_demo/server.py`）

| 位置 | 改动 |
|---|---|
| `import asyncio` | 新增导入 |
| `chat_completions` 非流式分支 | `run_chat(...)` → `await asyncio.to_thread(run_chat, req.messages, req.max_tokens)`，推理不再阻塞事件循环 |
| `run_chat(messages, max_tokens=None)` | 签名加参数；生成循环加 `(max_tokens is None or tok_num < max_tokens)` 条件 |
| `run_chat_stream(..., max_tokens=None)` | 同上 |
| `chat_completions` 流式分支 | 把 `req.max_tokens` 传给 `run_chat_stream` |
| `/health` | 返回 `{"status": "ok", "busy": _model_lock.locked()}`，busy 表示正在推理 |

### 后端客户端（`backend/core/`）

| 文件 | 改动 |
|---|---|
| `config.py` | `QWEN_TIMEOUT`（300）拆成 `QWEN_CONNECT_TIMEOUT`（5）/ `QWEN_READ_TIMEOUT`（600）；新增 `QWEN_MAX_TOKENS`（4096） |
| `qwen_client.py` | AsyncClient 改模块级单例复用；`check_health()` 返回 `{"reachable", "busy"}`；`chat()` 发送 `max_tokens`、区分超时/连接超时/连不上错误、记录 `Qwen 调用耗时 X.Xs` |

### 状态上报（`backend/services/` + `backend/api/`）

| 文件 | 改动 |
|---|---|
| `ai_service.py` | `get_status()` 用 `busy` 区分三种状态，message 对应准确文案 |
| `health_service.py` | 适配 `check_health()` 新返回结构，用 `["reachable"]` 作为 qwen 健康布尔值 |
| `api/ai.py` | `AIStatusResponse` 新增 `qwen_busy: bool \| None` 字段 |

---

## 五、API 变化

`GET /api/v1/ai/status` 返回值：

```json
{ "qwen_reachable": true, "qwen_busy": false, "qwen_url": "http://127.0.0.1:8000", "message": "Qwen 在线" }
{ "qwen_reachable": true, "qwen_busy": true,  "qwen_url": "http://127.0.0.1:8000", "message": "Qwen 推理引擎正在处理其它请求" }
{ "qwen_reachable": false, "qwen_busy": null, "qwen_url": "http://127.0.0.1:8000", "message": "Qwen 推理引擎不可达，请确认引擎已启动" }
```

新增 `qwen_busy` 字段，前端可据此显示"推理中"状态（如引擎忙碌标识）。

---

## 六、验证步骤

```bash
# 1. 重启推理引擎（改的是 server.py，必须重启才生效）
python server.py -m ../path/model.bmodel -c ../config --port 8000

# 2. 健康检查应秒回，且带 busy 标志
curl http://127.0.0.1:8000/health
# → {"status":"ok","busy":false}

# 3. 提交一个文档，处理进行中再查 health —— 应秒回 {"status":"ok","busy":true}（以前会超时）
curl -X POST http://localhost:8080/api/v1/documents/process -H "Content-Type: application/json" \
  -d '{"task_type":"accident_analysis","original_text":"某工厂发生火灾","requirements":"分析原因","output_filename":"t"}'

# 4. 看后端日志的 "Qwen 调用耗时 X.Xs"，用真实耗时评估是否要调 QWEN_READ_TIMEOUT
```

## 七、配置项一览

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `QWEN_CONNECT_TIMEOUT` | 5s | 连接超时，局域网内应 <1s |
| `QWEN_READ_TIMEOUT` | 600s | 读超时，8K 长推理可能几分钟 |
| `QWEN_MAX_TOKENS` | 4096 | 单次生成 token 上限，封顶最坏耗时 |

> ⚠️ 旧配置项 `QWEN_TIMEOUT` 已废弃，若环境变量里设过需改为 `QWEN_READ_TIMEOUT`。

---

## 八、尚未做的事（后续可选）

- **客户端走流式**：引擎的流式路径本来就不阻塞事件循环，客户端改成 `stream=True` + 解析 SSE，可获得渐进输出和实时进度判断，但 `chat()` 复杂度上升。
- **失败重试**：超时/失败的任务目前置 failed 后只能重新提交，可加 `POST /documents/{id}/retry` 把状态改回 queued 重新入队。
