# SafeRAG CI 落地计划（GitHub Actions）

> 状态：计划已定，workflow 已写；待推 GitHub 首跑修到绿、加分支保护。
> 背景：定位通用 RAG 中台、一体机盒子交付、前端在外部团队仓库。CI 是"能改 → 敢改"的守门员。

---

## 一、现状基线（2026-09-14 实测）

- **测试当前全绿**：`python -m pytest tests/ backend/tests/` → **35 passed, 1 skipped**。跳过的 1 条是 PDF 导出用例（需 reportlab），属"装了才跑"的可选测试，跳过正确。
- **文档过期已证实**：[docs/RAG-architecture.md](docs/RAG-architecture.md) 曾记"test_template_service 断言未同步"——实测该测试早已通过，记录过时。这正是 CI 的价值：测试不会过期。
- **本机缺 jieba，`import backend.main` 本机跑不了**：`retriever.py` 模块 import 就要 jieba。CI 会把这个坑变成常态门禁。
- **ruff 未装、全仓库 lint 无基线**：首跑 `ruff check .` 结果未知，可能需一次性对齐。

## 二、目标

把"变更风险"关在合并之前：每次 push/PR，GitHub 自动跑验证，主分支设保护——**CI 红了不许合并**。

## 三、三层验证分工（离线盒子的现实约束）

| 层 | 内容 | 位置 | 目的 |
|---|---|---|---|
| **L1 单元门禁** | pytest：parser / repo / service，全部 fixtures + 临时 SQLite，**自包含、不碰模型/网络** | GitHub Actions `ci.yml` | PR/推 main 快速挡业务逻辑回归 |
| **L2 应用冒烟** | `python -c "import backend.main"`：全模块 import 不崩 | GitHub Actions `ci.yml` | 挡"import 期 NameError 应用起不来"这类**真实发生过**的事故（见 devlog 2026-09-02） |
| **L3 真机回归** | TPU + bmodel + BGE + reranker 的检索/生成 + golden 质量 | 盒子侧：发布前冒烟脚本 | 需要真实模型与内网，GitHub 够不着；这是**有意的边界** |

## 四、依赖分层（新增 `backend/requirements-test.txt`）

已核实被测模块 import 图（`import backend.main` 所覆盖模块的第三方依赖合集）：

- **L1（快，约 30s 装完）**：fastapi / pydantic / pydantic_core / pytest / PyJWT
- **L2 追加（重件，走 pip 缓存）**：httpx / python-multipart / numpy / onnxruntime / tokenizers / chromadb / jieba / rank_bm25
- **不需要**：transformers / Pillow / jinja2 / PyYAML / pybind11 / python-docx / pypdf（③④ 组，只给视觉 demo 与重编 .so 用；reranker 模块 import 也只拉 onnxruntime，**不拉 transformers**）
- 理由：embedding 客户端**懒加载**模型文件（首次调用才读），故无模型的 CI 上 `import` 安全。
- **Python 固定 3.10**，对齐盒子上实测锁定的 3.10.12。

## 五、Workflow 设计

### `.github/workflows/ci.yml`（合并门禁）
- 触发：`push` 到 main + 任一 `pull_request`
- 两个并行 job：
  1. **lint**：ubuntu + py3.10 + `pip install ruff` + `ruff check .`
  2. **test**：`actions/setup-python@v5 (3.10, cache: pip)` → 装 `requirements-test.txt` → `pytest tests/ backend/tests/`（L1）→ `python -c "import backend.main"`（L2）
- `concurrency` 取消同分支旧跑，省额度。

### `.github/workflows/release.yml`（打 tag 出源码包）
- 触发：`push` tag `v*`
- 构建**源码 tarball**（排除 `.git / backend/data / models / Qwen3_5 / __pycache__`）+ `softprops/action-gh-release` 挂到 GitHub Release，带发布清单。
- **明确边界**：盒子侧产物（镜像 tar、deb、前端包）必须在本机构架（aarch64 + 模型 + 前端目录）用 `scripts/deploy/docker/build_images.sh` / `scripts/deploy/make_deb.sh` 产出，GitHub 托管 runner 产不了。后续若要把盒子产物 CI 化，需引入 aarch64 runner + 模型/前端仓库，暂不做。

## 六、ruff 收敛

- `pyproject.toml` 的 `target-version = "py312"` 与运行时 3.10 **不一致**：收窄到 `py310`，避免 CI 放行 py312 才合法的语法、盒子 3.10 一跑就炸。
- 全仓库 lint 无基线：首跑若红，做一次性对齐后再收紧规则。

## 七、实施步骤清单

- [x] 基线摸底：35 passed / 1 skipped；全模块 import 依赖图；ruff 未装
- [x] 新建 `backend/requirements-test.txt`（L1+L2 分层，版本对齐 requirements.txt）
- [x] `pyproject.toml`：ruff target-version → py310
- [x] 新建 `docs/CI-PLAN.md`（本文档）
- [x] 新建 `.github/workflows/ci.yml`
- [x] 新建 `.github/workflows/release.yml`（源码包初版）
- [ ] 推送 GitHub 首跑，按红修到绿
- [ ] 仓库设置 → 分支保护：main 必须 `CI` 校验通过

## 八、边界与暂不做（说清楚，避免误解）

- **golden 检索质量门禁**（[scripts/eval_retrieval.py](scripts/eval_retrieval.py) 接 BGE 模型）：需 ~100MB 模型下载，后置，成功后进 release job 或盒子侧。
- **盒子产物 CI 化**：需 aarch64 runner，暂不做。
- **前端 CI**：不属本仓库（外部团队维护），这边只承诺稳定 API 契约。