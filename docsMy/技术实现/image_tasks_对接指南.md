# image-tasks 接口对接指南

## 背景

本项目在 OpenAI 标准同步接口（`/v1/images/generations`、`/v1/images/edits`）之外，额外提供了一套**异步任务队列接口**（`/api/image-tasks/*`）。

两套接口对比：

| 维度 | 标准同步接口 `/v1/images/*` | 异步任务接口 `/api/image-tasks/*` |
|------|----------------------------|------------------------------------|
| 调用方式 | 同步阻塞，等结果 | 提交立即返回，轮询获取结果 |
| 超时风险 | HTTP 长时间等待可能超时 | 不存在，后台独立执行 |
| 重复提交 | 每次都会触发新任务 | 幂等，相同 ID 不重复执行 |
| 适合场景 | 简单集成、快速调用 | 前端长轮询、批量任务、队列管理 |

> 任务 ID（`client_task_id`）**由调用方自己生成传入**，服务端不自动分配，建议使用 UUID 或业务流水号。详细存储与幂等机制见 [image_tasks_接口分析.md](./image_tasks_接口分析.md)。

---

## 对接步骤

### 第一步：获取 API Key

向本项目管理员申请 API Key，调用时放在 HTTP Header：

```
Authorization: Bearer <your-api-key>
```

### 第二步：提交任务

根据业务类型选择接口。

---

#### 文生图任务

**接口：** `POST /api/image-tasks/generations`

**Content-Type：** `application/json`

**请求体：**

```json
{
  "client_task_id": "your-unique-task-id-001",
  "prompt": "一只在草地上奔跑的金毛犬，阳光明媚，高清写实风格",
  "model": "gpt-image-2",
  "size": "1024x1024",
  "quality": "auto"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `client_task_id` | string | ✅ | 调用方自定义唯一任务 ID，建议用 UUID |
| `prompt` | string | ✅ | 生图描述，支持中英文 |
| `model` | string | | 默认 `gpt-image-2` |
| `size` | string | | 如 `1024x1024`、`1792x1024`，不传由模型决定 |
| `quality` | string | | `auto`（默认）、`high`、`medium`、`low` |

**返回示例（立即返回，status 为 queued）：**

```json
{
  "id": "your-unique-task-id-001",
  "status": "queued",
  "mode": "generate",
  "model": "gpt-image-2",
  "size": "1024x1024",
  "quality": "auto",
  "created_at": "2026-05-29 10:00:00",
  "updated_at": "2026-05-29 10:00:00"
}
```

---

#### 图生图任务（图片编辑）

**接口：** `POST /api/image-tasks/edits`

支持两种 Content-Type：

---

**方式 A：JSON + 图片 URL（推荐）**

**Content-Type：** `application/json`

```json
{
  "client_task_id": "edit-task-002",
  "prompt": "将背景替换为雪山风景",
  "model": "gpt-image-2",
  "size": "1024x1024",
  "quality": "auto",
  "images": [
    {
      "image_url": "https://example.com/original.png"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `client_task_id` | string | ✅ | 调用方自定义唯一任务 ID |
| `prompt` | string | ✅ | 编辑指令 |
| `images` | array | ✅ | 图片引用列表，见下方格式说明 |
| `model` | string | | 默认 `gpt-image-2` |
| `size` | string | | 输出尺寸 |
| `quality` | string | | 默认 `auto` |

**图片引用支持的格式：**

```json
// 方式1：http/https URL
{ "image_url": "https://example.com/photo.jpg" }

// 方式2：Base64 内联
{ "b64_json": "iVBORw0KGgo...", "mime_type": "image/png", "filename": "photo.png" }

// 方式3：data URL
{ "image_url": "data:image/png;base64,iVBORw0KGgo..." }
```

> 注意：不支持 `file_id` 引用，单张图片最大 **50MB**。

---

**方式 B：multipart/form-data（适合直接上传文件）**

```
POST /api/image-tasks/edits
Content-Type: multipart/form-data

client_task_id=edit-task-003
prompt=将背景替换为雪山
model=gpt-image-2
image=<文件二进制>
```

表单字段说明：

| 字段名 | 说明 |
|--------|------|
| `client_task_id` | 任务 ID |
| `prompt` | 编辑指令 |
| `model` | 模型名 |
| `size` | 输出尺寸 |
| `quality` | 质量 |
| `image` / `image[]` / `images` / `images[]` / `image_url` | 图片文件或 URL |

---

### 第三步：轮询任务状态

**接口：** `GET /api/image-tasks?ids={task_id1},{task_id2}`

**示例：**

```
GET /api/image-tasks?ids=your-unique-task-id-001
Authorization: Bearer <your-api-key>
```

**返回示例（执行成功）：**

```json
{
  "items": [
    {
      "id": "your-unique-task-id-001",
      "status": "success",
      "mode": "generate",
      "model": "gpt-image-2",
      "size": "1024x1024",
      "quality": "auto",
      "created_at": "2026-05-29 10:00:00",
      "updated_at": "2026-05-29 10:00:15",
      "data": [
        { "url": "https://your-server.com/images/xxx.png" }
      ],
      "usage": {}
    }
  ],
  "missing_ids": []
}
```

**返回示例（执行失败）：**

```json
{
  "items": [
    {
      "id": "your-unique-task-id-001",
      "status": "error",
      "error": "号池中没有可用账号或所有账号均被限流，请检查号池状态",
      ...
    }
  ],
  "missing_ids": []
}
```

不传 `ids` 参数则返回当前用户的全部任务列表（按更新时间倒序）：

```
GET /api/image-tasks
```

---

### 第四步：轮询策略建议

推荐的轮询节奏：

```
提交任务 → 等待 3s → 第1次查询
未完成   → 等待 3s → 第2次查询
未完成   → 等待 5s → 第3次查询
未完成   → 等待 5s → 第4次查询
...
超过 120s 仍未完成 → 按超时处理
```

建议设置最大等待时间为 **120 秒**，超时后可重新提交（相同 `client_task_id` 如果原任务还在执行，则会拿到原任务状态）。

---

## 完整对接示例（Python）

```python
import time
import uuid
import requests

BASE_URL = "https://your-server.com"
API_KEY = "your-api-key"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def submit_generation(prompt: str, task_id: str = None) -> dict:
    """提交文生图任务"""
    task_id = task_id or str(uuid.uuid4())
    resp = requests.post(
        f"{BASE_URL}/api/image-tasks/generations",
        headers=HEADERS,
        json={
            "client_task_id": task_id,
            "prompt": prompt,
            "model": "gpt-image-2",
            "size": "1024x1024",
        },
    )
    resp.raise_for_status()
    return resp.json()


def poll_task(task_id: str, timeout: int = 120, interval: int = 3) -> dict:
    """轮询任务状态，直到完成或超时"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            f"{BASE_URL}/api/image-tasks",
            headers=HEADERS,
            params={"ids": task_id},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            raise RuntimeError(f"任务 {task_id} 不存在")
        task = items[0]
        status = task.get("status")
        if status == "success":
            return task
        if status == "error":
            # 服务重启导致任务中断，可重新提交
            error = task.get("error", "")
            raise RuntimeError(f"任务失败: {error}")
        time.sleep(interval)
    raise TimeoutError(f"任务 {task_id} 超时（{timeout}s）")


def generate_image(prompt: str) -> list[str]:
    """生成图片，返回图片 URL 列表"""
    task_id = str(uuid.uuid4())
    submit_generation(prompt, task_id)
    task = poll_task(task_id)
    return [item["url"] for item in task.get("data", [])]


if __name__ == "__main__":
    urls = generate_image("一只在草地上奔跑的金毛犬，阳光明媚，高清写实风格")
    print("生成的图片：", urls)
```

---

## 完整对接示例（TypeScript / fetch）

```typescript
const BASE_URL = "https://your-server.com";
const API_KEY = "your-api-key";

const headers = {
  Authorization: `Bearer ${API_KEY}`,
  "Content-Type": "application/json",
};

async function submitGeneration(prompt: string, taskId: string): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/image-tasks/generations`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      client_task_id: taskId,
      prompt,
      model: "gpt-image-2",
      size: "1024x1024",
    }),
  });
  if (!res.ok) throw new Error(`提交失败: ${res.status}`);
}

async function pollTask(
  taskId: string,
  timeoutMs = 120_000,
  intervalMs = 3_000
): Promise<{ url: string }[]> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const res = await fetch(`${BASE_URL}/api/image-tasks?ids=${taskId}`, { headers });
    if (!res.ok) throw new Error(`查询失败: ${res.status}`);
    const data = await res.json();
    const task = data.items?.[0];
    if (!task) throw new Error(`任务 ${taskId} 不存在`);
    if (task.status === "success") return task.data;
    if (task.status === "error") throw new Error(`任务失败: ${task.error}`);
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(`任务 ${taskId} 超时`);
}

async function generateImage(prompt: string): Promise<string[]> {
  const taskId = crypto.randomUUID();
  await submitGeneration(prompt, taskId);
  const data = await pollTask(taskId);
  return data.map((item) => item.url);
}
```

---

## 错误码说明

| HTTP 状态码 | 含义 | 处理建议 |
|-------------|------|----------|
| `400` | 参数错误（缺少必填字段、图片格式不支持等） | 检查请求参数 |
| `401` | API Key 无效或未传 | 检查 Authorization Header |
| `403` | 内容审核不通过 | 修改 prompt |
| `429` | 图片配额耗尽 | 等待配额恢复或联系管理员 |
| `502` | 上游服务异常 | 稍后重试 |

任务本身的执行错误（账号耗尽、被限流等）通过 `status: "error"` + `error` 字段返回，不走 HTTP 错误码。
