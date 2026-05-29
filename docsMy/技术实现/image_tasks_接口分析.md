# image_tasks 图片任务接口分析

## 概述

`api/image_tasks.py` 是图片生成任务的 HTTP 接口层，提供**异步任务队列**机制来处理文生图（文字生成图片）和图生图（图片编辑）两类请求。

与直接调用图片生成 API 不同，该接口采用"提交任务 → 轮询状态"的模式，客户端先提交任务拿到任务 ID，再通过查询接口获取执行结果，适合图片生成耗时较长的场景。

---

## 接口清单

| 方法 | 路径 | 功能 |
|------|------|------|
| `GET` | `/api/image-tasks` | 查询任务列表或指定任务状态 |
| `POST` | `/api/image-tasks/generations` | 提交文生图任务 |
| `POST` | `/api/image-tasks/edits` | 提交图生图（图片编辑）任务 |

---

## 接口详解

### 1. GET `/api/image-tasks` — 查询任务

**请求参数：**

| 参数 | 位置 | 说明 |
|------|------|------|
| `ids` | Query | 逗号分隔的任务 ID 列表，不传则返回当前用户的所有任务 |
| `Authorization` | Header | Bearer Token 身份验证 |

**返回格式：**
```json
{
  "items": [
    {
      "id": "task-001",
      "status": "success",
      "mode": "generate",
      "model": "gpt-image-2",
      "size": "1024x1024",
      "quality": "auto",
      "created_at": "2026-05-29 10:00:00",
      "updated_at": "2026-05-29 10:00:15",
      "data": [{"url": "https://..."}],
      "usage": {}
    }
  ],
  "missing_ids": ["not-found-id"]
}
```

**状态值说明：**

| 状态 | 含义 |
|------|------|
| `queued` | 已排队等待执行 |
| `running` | 正在执行中 |
| `success` | 执行成功，`data` 字段含图片 URL |
| `error` | 执行失败，`error` 字段含错误信息 |

---

### 2. POST `/api/image-tasks/generations` — 提交文生图任务

**请求体（JSON）：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `client_task_id` | string | ✅ | 客户端自定义任务 ID（幂等键） |
| `prompt` | string | ✅ | 生图描述文字 |
| `model` | string | | 模型名，默认 `gpt-image-2` |
| `size` | string | | 图片尺寸，如 `1024x1024` |
| `quality` | string | | 图片质量，默认 `auto` |

**幂等性：** 相同的 `client_task_id` 重复提交，直接返回已有任务，不会重复执行。

---

### 3. POST `/api/image-tasks/edits` — 提交图生图任务

支持 `multipart/form-data` 和 `application/json` 两种请求格式。

**请求字段（表单或 JSON）：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `client_task_id` | string | ✅ | 客户端自定义任务 ID |
| `prompt` | string | ✅ | 编辑指令文字 |
| `model` | string | | 模型名，默认 `gpt-image-2` |
| `size` | string | | 输出尺寸 |
| `quality` | string | | 输出质量，默认 `auto` |
| `image` / `images` / `image_url` | file 或 URL | ✅ | 原始图片，支持上传文件、http/https URL、data URL、Base64 |

---

## 实现架构

### 整体调用链

```
HTTP 请求
   │
   ▼
api/image_tasks.py          ← 接口路由层（鉴权、内容审核、参数解析）
   │
   ├── require_identity()   ← 身份验证（Bearer Token）
   ├── filter_or_log()      ← 内容审核（check_request）
   ├── parse_image_edit_request() ← 图片参数解析（仅图生图）
   │
   ▼
services/image_task_service.py  ← 任务服务层（任务状态机、并发控制、持久化）
   │
   ├── submit_generation() / submit_edit()  ← 提交任务
   ├── _submit()           ← 幂等写入任务、启动后台线程
   ├── _run_task()         ← 后台线程执行图片生成
   │
   ▼
services/protocol/          ← 协议适配层（实际调用上游图片 API）
   ├── openai_v1_image_generations.handle()
   └── openai_v1_image_edit.handle()
```

---

### 关键实现细节

#### 异步任务队列（后台线程模型）

`image_task_service._submit()` 方法在接受任务后，立即返回 `queued` 状态，同时通过 `threading.Thread` 启动后台线程异步执行。

```python
# 任务提交后立即返回，不等待执行结果
thread = threading.Thread(
    target=self._run_task,
    args=(key, mode, payload, dict(identity), model),
    daemon=True,
)
thread.start()
return _public_task(task)  # 此时 status == "queued"
```

#### 线程安全与持久化（RLock + JSON 文件）

所有任务数据存储在内存字典 `self._tasks` 中，同时持久化到 `DATA_DIR/image_tasks.json`。使用 `threading.RLock` 保证并发安全，写入通过原子替换（先写 `.tmp` 再 `rename`）避免文件损坏。

```
DATA_DIR/image_tasks.json       ← 任务持久化文件
DATA_DIR/image_tasks.json.tmp   ← 写入临时文件，写完后原子替换
```

#### 任务 ID 存储机制

**任务 ID 由调用方传入**（字段名 `client_task_id`），服务端不自动生成。

内存中以字典维护所有任务，key 格式为：

```
{owner_id}:{client_task_id}
```

- `owner_id`：发起请求的 API Key 在系统中对应的唯一用户 ID，从 `identity["id"]` 取得
- `client_task_id`：调用方自定义，建议使用 UUID 或业务流水号

不同用户可以使用相同的 `client_task_id`，因为 key 带了 `owner_id` 前缀，互不干扰。

**持久化：** 内存数据同步写入 `data/image_tasks.json`，写入采用原子替换策略，先写 `.tmp` 临时文件再 `rename` 覆盖，防止写入中途宕机导致文件损坏。

```
data/image_tasks.json        ← 正式任务数据文件
data/image_tasks.json.tmp    ← 原子写入临时文件，写完后替换正式文件
```

JSON 文件结构示例：

```json
{
  "tasks": [
    {
      "id": "your-task-id",
      "owner_id": "user-abc",
      "status": "success",
      "mode": "generate",
      "model": "gpt-image-2",
      "size": "1024x1024",
      "quality": "auto",
      "created_at": "2026-05-29 10:00:00",
      "updated_at": "2026-05-29 10:00:15",
      "data": [{ "url": "https://..." }],
      "usage": {}
    }
  ]
}
```

任务按 `updated_at` 倒序排列（最新的在前）。

#### 幂等提交机制

以 `owner_id:client_task_id` 作为唯一 key。相同 key 的任务已存在时直接返回，防止重复提交：

```python
key = f"{owner_id}:{client_task_id}"
task = self._tasks.get(key)
if task is not None:
    return _public_task(task)  # 直接返回已有任务
```

#### 服务重启恢复

服务启动时，`_recover_unfinished_locked()` 会将所有 `queued` / `running` 状态的任务标记为 `error`，原因为"服务已重启，未完成的图片任务已中断"，避免任务永久卡在中间状态。

#### 任务过期清理

`_cleanup_locked()` 在每次提交和查询时触发，自动清除超出保留天数（`config.image_retention_days`，默认 30 天）且已完成（`success` / `error`）的任务，防止数据无限增长。只有处于终态的任务才会被清除，进行中的任务不受影响。

---

### 图片来源支持（api/image_inputs.py）

图生图接口支持多种图片输入格式，解析逻辑在 `image_inputs.py`：

| 格式 | 说明 |
|------|------|
| 上传文件（multipart） | 通过 `image` / `image[]` / `images` 等字段上传 |
| HTTP/HTTPS URL | 自动下载，限制 50MB |
| data URL | `data:image/png;base64,...` 格式内联图片 |
| Base64 字符串 | 原始 Base64 编码图片数据 |
| JSON 对象引用 | `{"image_url": "https://..."}` 或 `{"b64_json": "..."}` |

不支持 `file_id` 引用（显式拒绝并返回 400）。

---

### 身份验证与内容审核

- **身份验证**：`require_identity()` 从 `Authorization: Bearer <token>` 中提取令牌，支持管理员 `auth_key` 和普通用户 Token 两种模式，验证失败返回 401。
- **内容审核**：`filter_or_log()` 在任务提交前调用 `check_request()` 检查 prompt，审核失败直接拒绝并记录日志。
- **日志记录**：任务执行完成（成功或失败）后，通过 `log_service` 写入调用日志，包含账号邮箱、耗时、图片 URL 等信息。

---

## 数据流图（文生图任务为例）

```
客户端
  │ POST /api/image-tasks/generations
  │ { client_task_id, prompt, model, ... }
  ▼
[鉴权] require_identity()
  │ 验证失败 → 401
  ▼
[内容审核] check_request(prompt)
  │ 审核失败 → 403/400
  ▼
[提交任务] image_task_service.submit_generation()
  │ 幂等判断：已存在 → 直接返回
  │ 新任务 → 写入 tasks["queued"]，启动后台线程
  │ 返回 { status: "queued", id: "..." }
  ▼
客户端轮询 GET /api/image-tasks?ids=xxx
  │
  ├── status: "running" → 继续等待
  │
  └── status: "success" → 获取 data[].url（图片链接）
      status: "error"   → 获取 error 字段（错误原因）

[后台线程]
  openai_v1_image_generations.handle(payload)
  │ 成功 → 更新 status="success", data=[{url}]
  └── 失败 → 更新 status="error", error="..."
```
