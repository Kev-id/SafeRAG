# 模板系统 + 逐节生成 · API 契约（给前端团队）

> 后端约定：本接口面由本仓库维护，前端按此对接。变更需同步本文档。
> 进度/结构核心变化：**报告不再一次性生成，而是"按节顺序生成、每节完成即查"**；
> 模板从"三选一"变成"可自定义、每用户一份"；单节可手动改、可追加要求/材料重生成。

---

## 1. 核心概念

- **模板（Template）**：一份有序的"节"列表。每节 = `{title, instruction}`（标题 + 撰写要求）。
  编号（一、二、三…）**由后端按顺序自动派生**——前端传节时不要带序号，删节/插节后编号自动重排。
- **归属**：模板分**系统示例**（`is_system=true`，人人都看得到、只读）和自己的（`is_system=false`）。
  系统示例固定 id：`accident_analysis` / `hazard_inspection` / `emergency_plan`。
- **文档产出**：提交时后端对模板做**快照**——之后改模板不影响已提交文档。
- **逐节进度**：生成过程中，详情接口的 `sections[].status` 会变化（queued→generating→completed/failed），
  前端轮询详情即可"边看边等"。
- **单节精修**：某节不满意 → 手动改（PATCH）或加"要求/材料"让 AI 只重生成这一节（revise）。

## 2. 模板 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/templates` | 我的 + 系统示例（系统示例排前） |
| POST | `/api/v1/templates` | 新建（归属当前用户） |
| GET | `/api/v1/templates/{id}` | 模板详情（仅本人/系统可见） |
| PATCH | `/api/v1/templates/{id}` | 改名字/描述/节列表（系统示例 403） |
| DELETE | `/api/v1/templates/{id}` | 删自己的（系统示例 403） |

节结构（GET 返回，`no` 为派生编号）：
```json
{ "id": "...", "name": "隐患排查报告", "description": "...",
  "is_system": false,
  "sections": [
    { "no": "一、", "title": "排查概况", "instruction": "排查时间与范围……" },
    { "no": "二、", "title": "隐患明细清单", "instruction": "逐条输出八要素……" }
  ],
  "created_at": "...", "updated_at": "..." }
```

POST/PATCH 请求体（节 **不带 no**）：
```json
{ "name": "我的模板", "description": "...",
  "sections": [ { "title": "总体情况", "instruction": "概括现场基本情况" } ] }
```

权限：GET=tous 登录即可；写需要业务权限（user/sysadmin/secadmin）。

## 3. 文档提交（改用 template_id）

`POST /api/v1/documents/process`：

```json
{ "template_id": "accident_analysis",   // 新用法：模板 id（自己或系统示例）
  "original_text": "事故描述……",
  "requirements": "生成规范公文",
  "output_filename": "事故分析报告",
  "provinces": [], "cities": [], "file_types": [] }
```

- `task_type` 仍接受（旧前端兼容，等价于走对应系统模板）；**新前端请用 template_id**。
- 返回 `{id, status, output_filename}`；`status` 初始 `queued`，随后后台逐节生成。

## 4. 文档详情（含逐节进度 + 单节结果）

`GET /api/v1/documents/{id}` 返回（新增字段）：

```json
{ "id": "...", "status": "processing", "output_filename": "事故分析报告.md",
  "template_id": "accident_analysis", "template_name": "事故分析报告",
  "original_text": "……", "requirements": "……",
  "sections": [
    { "no": "一、", "title": "基本情况", "instruction": "……",
      "content": "……已生成内容……", "status": "completed" },
    { "no": "二、", "title": "原因分析", "instruction": "……",
      "content": null, "status": "generating" },
    { "no": "三、", "title": "事故性质认定", "instruction": "……",
      "content": null, "status": "queued" }
  ],
  "report_content": "...整篇 .md（生成过程中仍为 null，最后一节完成才渲染）..." }
```

- `sections[].status` 轮询依据：`queued → generating → completed`（失败 `failed`）。
- 旧文档（模板化之前）`sections` 为 `null`，前端退回整篇 `report_content`。
- 单节失败不影响其它节；文档最终 `completed`（有任一节成功）或 `failed`（全失败）。

## 5. 单节编辑 / 精修

**手动改某一节**（前端画布保存）：

`PATCH /api/v1/documents/{id}/sections/{index}`
```json
{ "content": "用户改写后的本节内容" }
```
返回 `SectionResult`（见下）。改完整篇 .md 已重排，下载/Word 同步。

**追加要求/材料，让 AI 只重生成这一节**：

`POST /api/v1/documents/{id}/sections/{index}/revise`
```json
{ "requirements": "补充一点：要引用危化品条例", "materials": "补充材料文本……" }
```
- 精修**只基于本节自己内容**改进：prompt = 原文 + 总体要求 + **本节当前内容**（作改进底稿）
  + 用户本次补充要求/材料。**不注入其它章节、不重新检索法规**（暂定；需要引条文时前端可
  把条文直接贴进 `materials`）。`content` 只换本节。
- **精修进行中**：调用后先置该节 `status="generating"` 并落库——前端可轮询详情
  （同文档生成），看到该节"精修中"；完成后置回 `completed`（附 `revised_at`）；
  失败自动恢复原状态（旧内容保留）。
- 返回同步，直接得到新内容：
```json
{ "id": "...", "index": 0, "no": "一、", "title": "基本情况", "content": "新内容……", "status": "completed" }
```

## 6. 前端建议流程

1. 报告提交页：`GET /templates` 拉下拉框（系统示例 + 自己的）；选模板后 `POST /process`。
2. 详情/生成页：轮询 `GET /documents/{id}`（建议 2~5s），按 `sections[].status` 渲染"已完成"节内容、显示"生成中"节骨架。
3. 生成完成后：可"编辑模式"逐节改（PATCH），或点某节"AI 改进"（revise 弹窗填要求/材料）。
4. 下载仍走 `GET /documents/{id}/download`（.md / .docx）。

## 7. 不变项

- 认证：Bearer token（`Authorization` 或 `?token=` 下载场景）。
- 文档列表 `GET /documents`、删除、重试、统计接口不变（列表项新增 `template_id/template_name`）。
- 聊天 SSE 链路不变（单节精修走同步非流式，见 revise）。