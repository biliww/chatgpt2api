# image_tasks 图片存储与返回机制

本文说明 `api/image_tasks.py` 中几个图片任务接口配合生成图片时，图片如何落到本地、如何通过接口返回给调用方，以及上游原始图片结果到底是 URL、二进制流还是 Base64。

## 结论先行

1. `POST /api/image-tasks/generations` 和 `POST /api/image-tasks/edits` 提交任务时不会立即返回图片文件，只会返回任务状态。
2. 后台线程实际生成图片后，会把图片内容保存到 `data/images/YYYY/MM/DD/时间戳_md5.png`。
3. 任务查询接口 `GET /api/image-tasks` 返回的是图片访问 URL，例如 `/images/2026/06/02/xxx.png`，不是直接返回图片二进制。
4. 图片文件本体通过 `GET /images/{image_path}` 返回。若本地存在，走 `FileResponse`；若只存在 WebDAV，则从 WebDAV 拉取 bytes 后返回 `Response(content=..., media_type="image/png")`。
5. 对普通 `gpt-image-2` 生成链路来说，上游最终会先解析出图片下载 URL，再下载成二进制 bytes，随后转成 Base64 进入统一格式化逻辑，最后再 Base64 解码后落盘。
6. 对 `codex-gpt-image-2` 生成链路来说，上游响应里可直接出现 Base64 图片结果，代码会直接取 Base64，再解码落盘。
7. 所以对最终本地保存来说，不是把上游 URL 字符串保存到文件，而是保存真实图片 bytes。

## 涉及接口

`api/image_tasks.py` 中有三个接口：

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/api/image-tasks` | 查询任务状态和结果 |
| `POST` | `/api/image-tasks/generations` | 提交文生图异步任务 |
| `POST` | `/api/image-tasks/edits` | 提交图生图异步任务 |

这三个接口本身只负责鉴权、内容审核、参数解析和调用任务服务。图片生成、下载、保存、URL 组装都在后续服务层完成。

## 整体调用链

```text
POST /api/image-tasks/generations 或 /api/image-tasks/edits
    |
    v
api/image_tasks.py
    |
    v
services/image_task_service.py
    submit_generation() / submit_edit()
    |
    v
后台线程 _run_task()
    |
    v
services/protocol/openai_v1_image_generations.py
services/protocol/openai_v1_image_edit.py
    |
    v
services/protocol/conversation.py
    stream_image_outputs_with_pool()
    format_image_result()
    save_image_bytes()
    |
    v
services/image_storage_service.py
    image_storage_service.save()
    |
    v
data/images/YYYY/MM/DD/xxx.png
data/image_index.json
data/image_tasks.json
```

## 1. 图片如何存储到本地

图片真正落盘的位置在 `services/image_storage_service.py` 的 `ImageStorageService.save()`。

核心逻辑如下：

```python
rel = self.make_relative_path(image_data)
path = _local_image_path(rel)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(image_data)
```

相对路径由 `make_relative_path()` 生成：

```python
file_hash = hashlib.md5(image_data).hexdigest()
filename = f"{int(time.time())}_{file_hash}.png"
relative_dir = Path(time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"))
```

因此本地文件路径类似：

```text
data/images/2026/06/02/1780390000_5d41402abc4b2a76b9719d911017c592.png
```

注意几点：

- 文件扩展名固定为 `.png`。
- 文件名包含当前秒级时间戳和图片 bytes 的 MD5。
- 图片目录根路径来自 `config.images_dir`，也就是项目下的 `data/images`。
- 保存前会调用 `config.cleanup_old_images()` 清理超过保留天数的旧图片。
- 保存后会把索引信息写入 `data/image_index.json`，包含 `rel`、`path`、`name`、`date`、`size`、`created_at`、`storage`、`local`、`webdav`、`width`、`height` 等信息。

## 2. 如何通过接口返回给调用方

### 2.1 提交任务时返回任务状态

提交文生图或图生图任务时，`image_task_service._submit()` 会先写入一个任务记录，并启动后台线程：

```python
threading.Thread(target=self._run_task, ...).start()
return _public_task(task)
```

此时调用方拿到的是任务状态，不是图片：

```json
{
  "id": "task-001",
  "status": "queued",
  "mode": "generate",
  "model": "gpt-image-2",
  "size": "1024x1024",
  "quality": "auto",
  "created_at": "2026-06-02 10:00:00",
  "updated_at": "2026-06-02 10:00:00"
}
```

### 2.2 后台完成后把 URL 写入任务结果

后台线程 `_run_task()` 调用协议层后，拿到 `result["data"]`，并写回任务：

```python
self._update_task(key, status=TASK_STATUS_SUCCESS, data=data, usage=usage, error="")
```

任务持久化在：

```text
data/image_tasks.json
```

成功后的任务数据中，`data` 形如：

```json
{
  "tasks": [
    {
      "id": "task-001",
      "owner_id": "user-id",
      "status": "success",
      "mode": "generate",
      "model": "gpt-image-2",
      "size": "1024x1024",
      "quality": "auto",
      "data": [
        {
          "url": "https://your-server.com/images/2026/06/02/1780390000_xxx.png",
          "revised_prompt": "..."
        }
      ],
      "usage": {}
    }
  ]
}
```

### 2.3 查询任务接口返回图片 URL

调用方通过：

```http
GET /api/image-tasks?ids=task-001
```

拿到任务结果：

```json
{
  "items": [
    {
      "id": "task-001",
      "status": "success",
      "mode": "generate",
      "data": [
        {
          "url": "https://your-server.com/images/2026/06/02/1780390000_xxx.png",
          "revised_prompt": "..."
        }
      ]
    }
  ],
  "missing_ids": []
}
```

也就是说，`/api/image-tasks` 返回给调用方的是 JSON，其中图片字段是 URL。

### 2.4 图片 URL 如何返回文件本体

图片访问路由在 `api/system.py`：

```python
@router.get("/images/{image_path:path}", include_in_schema=False)
async def get_image(image_path: str):
    return get_image_response(image_path)
```

实际由 `services/image_service.py` 返回：

```python
def get_image_response(relative_path: str) -> FileResponse | Response:
    if image_storage_service.has_local(relative_path):
        return FileResponse(_safe_image_path(relative_path))
    return Response(content=image_storage_service.get_bytes(relative_path), media_type="image/png")
```

因此：

- 本地有文件：直接 `FileResponse` 返回本地图片文件。
- 本地没有但索引标记存在 WebDAV：通过 `image_storage_service.get_bytes()` 从 WebDAV 获取 bytes，再返回二进制响应。

## 3. 上游原始返回格式是什么

这里要区分“对外接口期望格式”和“内部真正拿到图片的方式”。

### 3.1 image_tasks 强制要求返回 URL

`ImageTaskService.submit_generation()` 和 `submit_edit()` 都会给协议层传：

```python
"response_format": "url"
```

这表示异步任务最终要给调用方返回图片 URL。

但这不代表上游原始结果只保存 URL。协议层为了生成这个 URL，会先拿到真实图片 bytes，并保存成本地文件。

### 3.2 普通 gpt-image-2 链路：先解析上游图片 URL，再下载 bytes

普通 `gpt-image-2` 走 `stream_image_outputs()`。

关键过程：

1. 通过 ChatGPT conversation 事件拿到 `conversation_id`、`file_ids`、`sediment_ids`。
2. 调用 `backend.resolve_conversation_image_urls(...)` 把这些 ID 解析为真实下载 URL。
3. 调用 `backend.download_image_bytes(image_urls)` 下载图片二进制。
4. 把下载到的 bytes 临时转成 Base64，变成统一的 `b64_json` 中间结构。
5. `format_image_result()` 再把 `b64_json` 解码成 bytes，调用 `save_image_bytes()` 保存。
6. 保存后只把本服务生成的 URL 放进 `data` 返回。

对应代码片段在 `services/protocol/conversation.py`：

```python
image_urls = backend.resolve_conversation_image_urls(conversation_id, file_ids, sediment_ids)
if image_urls:
    image_items = [
        {"b64_json": base64.b64encode(image_data).decode("ascii")}
        for image_data in backend.download_image_bytes(image_urls)
    ]
    data = format_image_result(... )["data"]
```

下载 bytes 的代码在 `services/openai_backend_api.py`：

```python
def download_image_bytes(self, urls: list[str]) -> list[bytes]:
    images = []
    for url in urls:
        response = self.session.get(url, timeout=120)
        ensure_ok(response, "image_download")
        if response.content not in images:
            images.append(response.content)
    return images
```

所以普通链路可以理解为：

```text
上游 conversation/file 引用
    -> 解析出上游下载 URL
    -> 下载图片 bytes
    -> 转 Base64 作为内部统一格式
    -> Base64 解码回 bytes
    -> 写入 data/images
    -> 返回本服务 /images/... URL
```

### 3.3 codex-gpt-image-2 链路：上游直接给 Base64

如果模型是 `codex-gpt-image-2`，会走 `stream_codex_image_outputs()`。

该逻辑会从上游响应中查找 `image_generation_call.result`：

```python
if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
    result = value["result"].strip()
    if result:
        return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
```

随后也会进入统一的 `format_image_result()`：

```python
data = format_image_result(
    [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
    ...
)["data"]
```

所以 Codex 图片链路可以理解为：

```text
上游直接返回 Base64 或 data:image/...;base64,...
    -> 提取 Base64
    -> Base64 解码成 bytes
    -> 写入 data/images
    -> 返回本服务 /images/... URL
```

### 3.4 format_image_result 如何决定返回 URL 还是 Base64

`format_image_result()` 是统一格式化出口：

```python
if response_format == "b64_json":
    data.append({
        "b64_json": b64_json,
        "url": save_image_bytes(base64.b64decode(b64_json), base_url),
        "revised_prompt": revised_prompt,
    })
else:
    data.append({
        "url": save_image_bytes(base64.b64decode(b64_json), base_url),
        "revised_prompt": revised_prompt,
    })
```

对 `/api/image-tasks/*` 来说，传入的是 `response_format=url`，所以任务结果只包含：

```json
{
  "url": ".../images/xxx.png",
  "revised_prompt": "..."
}
```

不会包含 `b64_json`。

但对于标准同步接口 `/v1/images/generations`、`/v1/images/edits`，如果调用方请求 `response_format=b64_json`，返回中会同时带 `b64_json` 和本服务保存后的 `url`。这是同步接口和异步任务接口的一个差异。

## 4. 图生图输入图片与生成结果图片的区别

图生图接口 `/api/image-tasks/edits` 会先读取调用方提供的参考图：

```python
payload, image_sources = await parse_image_edit_request(request)
images = await read_image_sources(image_sources)
```

参考图支持：

- multipart 上传文件：直接读取上传文件 bytes。
- http/https 图片 URL：服务端先下载成 bytes。
- data URL：解码成 bytes。
- 普通 Base64：解码成 bytes。

这些参考图 bytes 会通过 `encode_images()` 转成 Base64 后发给上游：

```python
def encode_images(images):
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]
```

但这里说的是“输入参考图”的处理，不是“生成结果图”的落盘。生成结果图仍然按前文流程保存到 `data/images` 并返回 URL。

## 5. URL 生成规则

保存图片后，`ImageStorageService._public_url()` 生成对外 URL：

```python
public_base_url = settings.get("public_base_url")
if public_base_url:
    return f"{public_base_url.rstrip('/')}/{rel}"
return f"{(base_url or config.base_url).rstrip('/')}/images/{rel}"
```

优先级：

1. 如果图片存储配置里设置了 `image_storage.public_base_url`，直接返回该公开 CDN/WebDAV 基础地址拼出的 URL。
2. 否则返回当前服务的 `/images/{rel}` 代理地址。

`base_url` 来自 `api/support.py` 的 `resolve_image_base_url(request)`，通常根据当前请求地址或配置中的 `base_url` 得出。

## 6. 存储模式

图片存储支持三种模式：

| mode | 保存位置 | 返回方式 |
|------|----------|----------|
| `local` | 只保存到 `data/images` | 返回本服务 `/images/...` 或配置的公开 URL |
| `webdav` | 只上传 WebDAV | 返回公开 URL；如果未配置公开地址，则 `/images/...` 会代理读取 WebDAV bytes |
| `both` | 本地和 WebDAV 都保存 | 优先本地 `FileResponse`，同时索引记录 WebDAV |

如果 `image_storage.enabled` 为 `false`，配置会归一化为 `local`。

## 7. 回答三个问题

### 1. 是如何存储到本地的？

生成结果最终都会变成图片 bytes，然后通过 `image_storage_service.save(image_data, base_url)` 保存到：

```text
data/images/YYYY/MM/DD/时间戳_md5.png
```

同时写入索引：

```text
data/image_index.json
```

任务结果写入：

```text
data/image_tasks.json
```

### 2. 如何通过接口返回给调用方？

调用方先提交任务，再轮询：

```http
GET /api/image-tasks?ids=任务ID
```

任务成功后返回 JSON：

```json
{
  "data": [
    {
      "url": "https://your-server.com/images/YYYY/MM/DD/xxx.png",
      "revised_prompt": "..."
    }
  ]
}
```

调用方再访问这个 `url` 获取图片文件本体。

### 3. 原始接口返回的图片是 URL、下载到本地保存，还是二进制流保存？

分情况：

- 普通 `gpt-image-2`：上游不是直接把最终图片 bytes 塞进任务接口响应里，而是先通过 conversation/file/sediment 引用解析出上游下载 URL；本项目再请求这些 URL，拿到 `response.content` 二进制 bytes，保存成本地图片。
- `codex-gpt-image-2`：上游响应中可能直接包含 Base64 或 data URL；本项目提取 Base64 后解码成 bytes，保存成本地图片。
- 对本项目的最终保存来说，保存的是图片二进制 bytes，不是保存 URL 文本，也不是把 Base64 字符串当文件内容写入。

最终返回给调用方的是本项目生成的图片访问 URL。
