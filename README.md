# SafeRAG

安全生产领域的 AI 报告生成后端服务：接收事故/隐患文本，调用本地部署的 Qwen 大模型，生成结构化的安全报告。

部署环境：Sophgo BM1688 ARM 盒子（内网）。Qwen3.5-4B 走 TPU 推理（端口 8000），本服务跑在端口 8080。

## 功能

- **多任务模板**：三种任务类型（事故分析 / 隐患排查 / 应急预案），前端选择后 Prompt 自动切换
- **异步处理**：提交文档后立即返回，推理在后台执行，前端轮询状态查询结果
- **分层存储**：元数据 + 原文存 SQLite，报告正文存 `.md` 文件
- **完整文档生命周期**：列表 / 详情 / 下载 / 删除

## 架构

采用 Router → Service → Repository 三层架构：

```
api/            路由层：参数校验、调用 Service、返回响应（不含业务逻辑）
services/       业务层：Prompt 构造、调用 Qwen、流程编排
repositories/   数据层：SQLite 读写 + 报告文件管理
```

外加三个基础设施模块：

- `config.py` — 集中配置（环境变量可覆盖）
- `database.py` — SQLite 连接管理与建表
- `qwen_client.py` — Qwen 推理引擎的异步 HTTP 客户端

## 目录结构

```
SafeRAG/
├── backend/
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 配置
│   ├── database.py             # SQLite 连接管理
│   ├── qwen_client.py          # Qwen HTTP 客户端
│   ├── api/                    # 路由层
│   │   ├── ai.py
│   │   ├── documents.py
│   │   └── tasks.py
│   ├── services/               # 业务层
│   │   ├── ai_service.py
│   │   ├── document_service.py
│   │   └── template_service.py # Prompt 模板
│   ├── repositories/           # 数据层
│   │   └── document_repo.py
│   └── requirements.txt
├── tests/                      # pytest 测试
├── data/                       # 运行时生成（SQLite + 文档），已被 git 忽略
└── devlog.md                   # 开发日志
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r backend/requirements.txt

# 2. 启动后端（需先确保 Qwen 推理引擎在 :8000 运行）
uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QWEN_BASE_URL` | `http://127.0.0.1:8000` | Qwen 推理引擎地址 |
| `QWEN_MODEL` | `tpu-qwen3.5` | 模型名 |
| `QWEN_TIMEOUT` | `300` | 调用超时（秒） |
| `DATA_DIR` | `../data` | 数据目录 |
| `DATABASE_URL` | `sqlite:///{DATA_DIR}/saferag.db` | SQLite 路径 |
| `HOST` | `0.0.0.0` | 后端监听地址 |
| `PORT` | `8080` | 后端端口 |
| `DEBUG` | `false` | 调试模式 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/ai/status` | Qwen 引擎连通性检查 |
| GET | `/api/v1/tasks` | 任务类型列表（前端下拉框） |
| GET | `/api/v1/tasks/{key}` | 单个任务类型信息 |
| POST | `/api/v1/documents/process` | 提交文档处理（异步，返回 id） |
| GET | `/api/v1/documents` | 文档列表（分页 + 状态过滤） |
| GET | `/api/v1/documents/{id}` | 文档详情（含报告正文） |
| GET | `/api/v1/documents/{id}/download` | 下载报告 `.md` |
| DELETE | `/api/v1/documents/{id}` | 删除文档 |

## 测试

```bash
python -m pytest
```

## 开发日志

开发过程中的决策与踩坑记录见 [devlog.md](devlog.md)。
