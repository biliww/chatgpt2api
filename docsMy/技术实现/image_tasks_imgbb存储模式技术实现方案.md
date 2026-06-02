# image_tasks imgbb 存储模式技术实现方案

本文说明如何在现有图片任务链路中新增 `imgbb` 图床存储模式，使 `/api/image-tasks/*` 任务成功后最终返回 imgbb 图片地址，同时尽量减少对现有 `local`、`webdav`、`both` 逻辑的侵入，便于后续从 `main` 合并到 `main-my` 时降低冲突。

## 目标

新增一种图片存储模式：

```text
image_storage.mode = "imgbb"
```

当图片任务生成结果后：

1. 图片 bytes 不再只保存成本地 `/images/...` 地址。
2. 新增 imgbb 上传类，把图片上传到 `https://api.imgbb.com/1/upload`。
3. 任务结果 `data[].url` 返回 imgbb 响应中的图片直链，优先使用 `data.url`。
4. 现有 `local`、`webdav`、`both` 模式保持原有行为不变。

## imgbb API 要点

上传接口：

```http
POST https://api.imgbb.com/1/upload
```

参数：

| 参数 | 必填 | 说明 |
|------|------|------|
| `key` | 是 | imgbb API Key |
| `image` | 是 | 二进制文件、Base64 数据或图片 URL，最大 32 MB |
| `name` | 否 | 文件名 |
| `expiration` | 否 | 自动删除秒数，范围 `60-15552000` |

建议使用 `POST multipart/form-data` 上传本地文件或 bytes，避免 GET 长度和编码问题。

成功响应核心字段：

```json
{
  "data": {
    "url": "https://i.ibb.co/w04Prt6/c1f64245afb2.gif",
    "display_url": "https://i.ibb.co/98W13PY/c1f64245afb2.gif",
    "url_viewer": "https://ibb.co/2ndCYJK",
    "delete_url": "https://ibb.co/2ndCYJK/..."
  },
  "success": true,
  "status": 200
}
```

本项目建议最终返回：

```text
data.url
```

如果 `data.url` 缺失，可降级为 `data.display_url`，再降级为 `data.url_viewer`。

## 现有图片任务链路

当前 `/api/image-tasks/*` 的生成结果落盘和返回路径是：

```text
api/image_tasks.py
    |
    v
services/image_task_service.py
    |
    v
services/protocol/openai_v1_image_generations.py
services/protocol/openai_v1_image_edit.py
    |
    v
services/protocol/conversation.py
    format_image_result()
    save_image_bytes()
    |
    v
services/image_storage_service.py
    image_storage_service.save()
    |
    v
返回 StoredImage.url
```

所以 imgbb 最合适的接入点是：

```python
image_storage_service.save(image_data, base_url).url
```

只要 `save()` 在 `mode == "imgbb"` 时返回 imgbb URL，任务层不需要改动，`/api/image-tasks` 自然会返回 imgbb 地址。

## 新增配置

建议扩展 `config.json` 中的 `image_storage`：

```json
{
  "image_storage": {
    "enabled": true,
    "mode": "imgbb",
    "webdav_url": "",
    "webdav_username": "",
    "webdav_password": "",
    "webdav_root_path": "chatgpt2api/images",
    "public_base_url": "",
    "imgbb_key": "YOUR_CLIENT_API_KEY",
    "imgbb_expiration": 0
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
|------|------|------|
| `imgbb_key` | string | imgbb API Key，`mode=imgbb` 时必填 |
| `imgbb_expiration` | int | 可选，自动删除秒数；`0` 表示不传该参数 |

`imgbb_expiration` 校验建议：

- `0`：不启用过期删除。
- `60-15552000`：传给 imgbb。
- 其他值：保存配置时报错。

## 配置层最小改动

当前 `services/config.py` 的 `_normalize_image_storage_settings()` 只允许：

```python
{"local", "webdav", "both"}
```

需要扩展为：

```python
{"local", "webdav", "both", "imgbb"}
```

同时增加字段归一化：

```python
"imgbb_key": str(source.get("imgbb_key") or "").strip(),
"imgbb_expiration": _normalize_positive_int(source.get("imgbb_expiration"), 0, 0),
```

校验逻辑从“启用后必须填写 WebDAV”改为按模式校验：

```python
def _validate_image_storage_settings(settings: dict[str, object]) -> None:
    if not _normalize_bool(settings.get("enabled"), False):
        return
    mode = str(settings.get("mode") or "local").strip().lower()
    if mode in {"webdav", "both"}:
        if not str(settings.get("webdav_url") or "").strip():
            raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV URL")
        if not str(settings.get("webdav_password") or "").strip():
            raise ValueError("启用 WebDAV 图片存储后必须填写 WebDAV 密码")
    if mode == "imgbb":
        if not str(settings.get("imgbb_key") or "").strip():
            raise ValueError("启用 imgbb 图片存储后必须填写 imgbb API Key")
        expiration = int(settings.get("imgbb_expiration") or 0)
        if expiration and not 60 <= expiration <= 15552000:
            raise ValueError("imgbb 过期时间必须为 60-15552000 秒，或填 0 表示不过期")
```

这样不会影响未启用、`local`、`webdav`、`both` 的已有配置。

## 新增类设计

为了尽量不修改原有代码，建议新增文件：

```text
services/imgbb_storage_service.py
```

新增类：

```python
class ImgbbStorageClient:
    """负责调用 imgbb 图床 API 上传图片，并解析返回的公开访问地址。"""
```

根据项目开发规范，新类和方法需要中文注释。建议实现结构如下：

```python
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from curl_cffi import requests

from services.image_storage_service import ImageStorageError


class ImgbbStorageClient:
    """负责调用 imgbb 图床 API 上传图片，并解析返回的公开访问地址。"""

    def __init__(self, settings: dict[str, object]):
        """初始化 imgbb 客户端，读取 API Key 和过期时间配置。"""
        self.key = str(settings.get("imgbb_key") or "").strip()
        self.expiration = int(settings.get("imgbb_expiration") or 0)
        self.endpoint = "https://api.imgbb.com/1/upload"
        self.session = requests.Session()

    def upload(self, image_data: bytes, *, name: str = "") -> dict[str, object]:
        """上传图片 bytes 到 imgbb，并返回标准化后的上传结果。"""
        if not self.key:
            raise ImageStorageError("imgbb API Key is required")
        if not image_data:
            raise ImageStorageError("image data is empty")
        if len(image_data) > 32 * 1024 * 1024:
            raise ImageStorageError("imgbb image exceeds 32MB limit")

        upload_name = name or hashlib.md5(image_data).hexdigest()
        params: dict[str, object] = {"key": self.key}
        if self.expiration:
            params["expiration"] = self.expiration

        response = self.session.post(
            self.endpoint,
            params=params,
            files={"image": (f"{upload_name}.png", image_data, "image/png")},
            timeout=60,
        )
        return self._parse_response(response)

    def _parse_response(self, response: requests.Response) -> dict[str, object]:
        """解析 imgbb 响应，校验成功状态并提取图片地址。"""
        try:
            payload = response.json()
        except Exception as exc:
            raise ImageStorageError(f"imgbb upload failed: invalid JSON response") from exc

        if response.status_code >= 400 or not payload.get("success"):
            message = str(payload.get("error") or payload.get("message") or response.status_code)
            raise ImageStorageError(f"imgbb upload failed: {message}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ImageStorageError("imgbb upload failed: missing data")

        url = str(data.get("url") or data.get("display_url") or data.get("url_viewer") or "").strip()
        if not url:
            raise ImageStorageError("imgbb upload failed: missing image url")

        return {
            "url": url,
            "display_url": str(data.get("display_url") or ""),
            "url_viewer": str(data.get("url_viewer") or ""),
            "delete_url": str(data.get("delete_url") or ""),
            "id": str(data.get("id") or ""),
            "size": int(data.get("size") or 0),
            "raw": data,
        }
```

说明：

- 新类只负责 imgbb API，不掺入本地/WebDAV 逻辑。
- 用 `files={"image": (...)}` 走 `multipart/form-data`，符合 imgbb 推荐。
- `ImageStorageError` 复用现有异常类型，方便 API 层保持原错误处理方式。
- 返回标准化 dict，便于写入 `data/image_index.json`。

## ImageStorageService 接入方式

为了减少冲突，不建议重构整个 `ImageStorageService`。只在 `save()` 里新增一个很小的分支：

```python
if mode == "imgbb":
    result = ImgbbStorageClient(self.settings()).upload(image_data, name=Path(rel).stem)
    dimensions = _image_dimensions(image_data)
    item = {
        "rel": rel,
        "path": rel,
        "name": Path(rel).name,
        "date": "-".join(rel.split("/")[:3]),
        "size": len(image_data),
        "created_at": _now_iso(),
        "storage": "imgbb",
        "local": False,
        "webdav": False,
        "imgbb": True,
        "remote_url": result["url"],
        "imgbb_url": result["url"],
        "imgbb_display_url": result["display_url"],
        "imgbb_viewer_url": result["url_viewer"],
        "imgbb_delete_url": result["delete_url"],
        "imgbb_id": result["id"],
    }
    if dimensions:
        item["width"], item["height"] = dimensions
    with self._index_lock:
        items = self._load_clean_index()
        items[rel] = item
        self._save_index(items)
    return StoredImage(rel=rel, url=str(result["url"]), storage="imgbb", size=len(image_data))
```

这一段只影响 `mode == "imgbb"`，不会改变 `local`、`webdav`、`both`。

## 图片索引结构

imgbb 模式下，`data/image_index.json` 建议记录：

```json
{
  "items": {
    "2026/06/02/1780390000_xxx.png": {
      "rel": "2026/06/02/1780390000_xxx.png",
      "path": "2026/06/02/1780390000_xxx.png",
      "name": "1780390000_xxx.png",
      "date": "2026-06-02",
      "size": 123456,
      "created_at": "2026-06-02 10:00:00",
      "storage": "imgbb",
      "local": false,
      "webdav": false,
      "imgbb": true,
      "remote_url": "https://i.ibb.co/xxx/image.png",
      "imgbb_url": "https://i.ibb.co/xxx/image.png",
      "imgbb_display_url": "https://i.ibb.co/yyy/image.png",
      "imgbb_viewer_url": "https://ibb.co/zzz",
      "imgbb_delete_url": "https://ibb.co/zzz/delete-token",
      "imgbb_id": "zzz",
      "width": 1024,
      "height": 1024
    }
  }
}
```

`remote_url` 用于保持和 WebDAV 的概念一致，`imgbb_*` 字段保存 imgbb 专有信息。

## list_items 兼容建议

当前 `list_items()` 里会判断：

```python
local = _local_image_path(rel).is_file()
webdav = bool(item.get("webdav"))
if not local and not webdav:
    indexed.pop(rel, None)
```

新增 imgbb 后，需要把 `imgbb` 也算作一种存在的远程存储：

```python
imgbb = bool(item.get("imgbb"))
if not local and not webdav and not imgbb:
    indexed.pop(rel, None)
```

storage 计算建议：

```python
storage = (
    "both" if local and webdav
    else "imgbb" if imgbb
    else "webdav" if webdav
    else "local"
)
```

返回 URL 时，如果 `storage == "imgbb"`，应优先返回索引里的 `imgbb_url` 或 `remote_url`：

```python
"url": str(item.get("imgbb_url") or item.get("remote_url") or self._public_url(rel, base_url))
```

这样后台图片管理列表也能直接显示 imgbb 图片地址。

## get_bytes 与 /images 代理行为

imgbb 模式下，任务结果会直接返回 imgbb URL，调用方通常不会访问本项目 `/images/...`。

是否支持 `/images/{rel}` 代理读取 imgbb，有两种方案：

### 方案 A：不代理 imgbb，推荐

`get_bytes()` 不处理 imgbb。原因：

- imgbb 已经返回公开图片 URL。
- `/api/image-tasks` 返回的是 imgbb 直链。
- 减少服务端转发流量和失败面。
- 改动最少。

这种情况下，如果有人拿 imgbb 模式的 `rel` 访问 `/images/{rel}`，可能返回 404。这是可以接受的，因为任务结果没有暴露这个本地代理 URL。

### 方案 B：代理读取 imgbb

如果希望后台下载、缩略图等功能继续依赖 `get_bytes()`，可以在 `get_bytes()` 中增加：

```python
if item.get("imgbb") and item.get("imgbb_url"):
    response = requests.get(str(item["imgbb_url"]), timeout=60)
    ensure status ok
    return bytes(response.content)
```

但这会增加对 `image_storage_service.py` 的改动，也会让服务器承担 imgbb 图片中转流量。为了降低合并冲突，建议第一期采用方案 A。

## 删除行为

现有 `delete()` 支持删除本地和 WebDAV。

imgbb 返回 `delete_url`，理论上可以调用该 URL 删除远程图片。但 imgbb 的删除 URL 是敏感管理链接，需要谨慎保存和使用。

第一期建议：

- 后台删除只从 `data/image_index.json` 移除索引。
- 不自动请求 `delete_url` 删除 imgbb 远端图片。
- 文档中说明：如果启用 `expiration`，由 imgbb 自动过期删除；如果未启用，则远端图片会保留。

后续如果需要完整删除能力，再新增 `ImgbbStorageClient.delete(delete_url)` 方法，并在 `delete()` 中按配置决定是否调用。

## sync_all 和 test_webdav 的处理

当前 `api/system.py` 有：

```text
POST /api/images/storage/test
POST /api/images/storage/sync
```

现有实现偏 WebDAV：

- `test_webdav()`
- `sync_all()`

新增 imgbb 后建议：

1. 新增 `ImageStorageService.test_storage()`，根据 mode 分发：
   - `webdav` / `both`：调用原 `test_webdav()`
   - `imgbb`：上传一个 1x1 PNG 测试图，成功后返回 URL
   - `local`：返回本地可写检测结果
2. 为减少冲突，第一期也可以暂不改接口，只在设置保存时做 imgbb key 校验；后续再统一测试按钮。

更推荐的最小方案：

- 新增 `ImgbbStorageClient.test()` 方法。
- `ImageStorageService.test_webdav()` 内部保留原方法名，但根据 `mode == "imgbb"` 调用 imgbb test。
- 这样 `api/system.py` 不需要改动。

## 前端配置改动

如果需要在设置页可配置 imgbb，需要改：

```text
web/src/lib/api.ts
web/src/app/settings/store.ts
web/src/app/settings/components/config-card.tsx
```

最小变更：

1. `ImageStorageMode` 增加 `"imgbb"`。
2. `ImageStorageSettings` 增加：
   - `imgbb_key: string`
   - `imgbb_expiration: number`
3. 设置页模式下拉增加：
   - `仅 imgbb 图床`
4. 当 `mode === "imgbb"` 时显示 imgbb API Key 和 expiration 输入框。
5. WebDAV 字段只在 `mode === "webdav" || mode === "both"` 时作为主要配置项。

为了降低合并冲突，前端可以后置；第一期可以通过直接编辑 `config.json` 或调用 `/api/settings` 配置。

## image_tasks 返回效果

配置：

```json
{
  "image_storage": {
    "enabled": true,
    "mode": "imgbb",
    "imgbb_key": "YOUR_CLIENT_API_KEY",
    "imgbb_expiration": 0
  }
}
```

提交任务：

```http
POST /api/image-tasks/generations
```

轮询结果：

```http
GET /api/image-tasks?ids=task-001
```

返回：

```json
{
  "items": [
    {
      "id": "task-001",
      "status": "success",
      "mode": "generate",
      "data": [
        {
          "url": "https://i.ibb.co/w04Prt6/generated.png",
          "revised_prompt": "..."
        }
      ]
    }
  ],
  "missing_ids": []
}
```

这里的 `data[0].url` 就是 imgbb 图片直链。

## 错误处理

建议错误策略：

| 场景 | 处理 |
|------|------|
| 未配置 `imgbb_key` | 保存配置时报错，或上传时报 `ImageStorageError` |
| 图片超过 32 MB | 上传前直接报错 |
| imgbb HTTP 非 2xx | 抛 `ImageStorageError("imgbb upload failed: ...")` |
| imgbb 返回 `success=false` | 抛 `ImageStorageError` |
| 响应缺失 URL | 抛 `ImageStorageError("missing image url")` |

在 image task 模式下，这些异常会被 `_run_task()` 捕获，任务状态变为：

```json
{
  "status": "error",
  "error": "imgbb upload failed: ..."
}
```

## 最小代码改动清单

建议第一期代码改动控制在：

```text
services/imgbb_storage_service.py              新增
services/config.py                             少量修改：mode、配置字段、校验
services/image_storage_service.py              少量修改：mode 分支、索引兼容、可选 test 分发
web/src/lib/api.ts                             可选，前端配置需要时修改
web/src/app/settings/store.ts                  可选，前端配置需要时修改
web/src/app/settings/components/config-card.tsx 可选，前端配置需要时修改
```

其中核心后端只需要前三项。

## 推荐实现顺序

1. 新增 `services/imgbb_storage_service.py`，实现 `ImgbbStorageClient.upload()` 和 `test()`。
2. 修改 `services/config.py`，支持 `mode=imgbb`、`imgbb_key`、`imgbb_expiration`。
3. 修改 `services/image_storage_service.py`，在 `save()` 中为 `imgbb` 增加上传分支。
4. 修改 `list_items()`，让 imgbb 索引不会被当成失效记录清理。
5. 使用 `/api/settings` 配置 imgbb。
6. 调用 `/api/image-tasks/generations` 生成图片，轮询确认 `data[].url` 为 `https://i.ibb.co/...`。
7. 可选：补前端设置项。

## 与现有模式的关系

`imgbb` 是新增模式，不改变已有模式语义：

| mode | 行为 |
|------|------|
| `local` | 保存到 `data/images`，返回 `/images/...` |
| `webdav` | 上传 WebDAV，返回公开地址或 `/images/...` 代理地址 |
| `both` | 本地和 WebDAV 都保存 |
| `imgbb` | 上传 imgbb，返回 imgbb 图片直链 |

如果后续希望“本地 + imgbb”双写，不建议复用 `both`，可以再新增：

```text
mode = "local_imgbb"
```

这样不会破坏现有 `both == local + webdav` 的含义。

## 方案取舍

为了降低冲突和维护成本，本方案选择：

- 新增 `ImgbbStorageClient` 独立类。
- 不重构 `ImageStorageService` 的整体结构。
- 不改 `image_task_service`、`conversation.py`、`api/image_tasks.py`。
- 让原有 `image_storage_service.save()` 继续作为唯一出口。
- 在 `mode=imgbb` 时直接返回 imgbb URL，使 task 模式自然返回图床地址。

这样新增功能主要集中在新类和少量配置/分发逻辑上，后续从 `main` 合并到 `main-my` 时冲突面较小。
