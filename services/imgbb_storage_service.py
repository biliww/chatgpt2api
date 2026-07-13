from __future__ import annotations

import base64
import hashlib
import random
from typing import Any

from curl_cffi import CurlMime, requests

from services.image_storage_service import ImageStorageError

IMGBB_UPLOAD_ENDPOINT = "https://api.imgbb.com/1/upload"
IMGBB_MAX_IMAGE_BYTES = 32 * 1024 * 1024
IMGBB_MIN_EXPIRATION_SECONDS = 60
IMGBB_MAX_EXPIRATION_SECONDS = 15552000
IMGBB_TEST_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class ImgbbStorageClient:
    """负责调用 imgbb 图床 API 上传图片，并解析返回的公开访问地址。"""

    def __init__(self, settings: dict[str, object]):
        """初始化 imgbb 客户端，读取 API Key（支持逗号分隔多个 Key，随机使用）和可选过期时间。"""
        raw_keys = str(settings.get("imgbb_key") or "").strip()
        self.keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        self.key = self.keys[0] if self.keys else ""
        self.expiration = int(settings.get("imgbb_expiration") or 0)
        self.endpoint = IMGBB_UPLOAD_ENDPOINT

    def _pick_key(self) -> str:
        """从多个 Key 中随机选择一个；兼容单 Key 场景。"""
        if not self.keys:
            raise ImageStorageError("imgbb API Key is required")
        return random.choice(self.keys)

    def upload(self, image_data: bytes, *, name: str = "", expiration: int | None = None) -> dict[str, object]:
        """上传图片 bytes 到 imgbb，随机选择一个 API Key，并返回标准化后的上传结果。"""
        if not self.keys:
            raise ImageStorageError("imgbb API Key is required")
        if not image_data:
            raise ImageStorageError("image data is empty")
        if len(image_data) > IMGBB_MAX_IMAGE_BYTES:
            raise ImageStorageError("imgbb image exceeds 32MB limit")

        resolved_expiration = self.expiration if expiration is None else expiration
        if resolved_expiration and not IMGBB_MIN_EXPIRATION_SECONDS <= resolved_expiration <= IMGBB_MAX_EXPIRATION_SECONDS:
            raise ImageStorageError("imgbb expiration must be between 60 and 15552000 seconds")
        upload_name = name or hashlib.md5(image_data).hexdigest()
        params: dict[str, object] = {"key": self._pick_key()}
        if resolved_expiration:
            params["expiration"] = resolved_expiration

        multipart = CurlMime()
        multipart.addpart(name="image", filename=f"{upload_name}.png", content_type="image/png", data=image_data)
        try:
            response = requests.post(self.endpoint, params=params, multipart=multipart, timeout=60)
        finally:
            multipart.close()
        return self._parse_response(response)

    def test(self) -> dict[str, object]:
        """上传一张最小测试图，验证 imgbb API Key 和上传链路是否可用。"""
        try:
            result = self.upload(IMGBB_TEST_IMAGE, name="chatgpt2api-imgbb-test", expiration=self.expiration or IMGBB_MIN_EXPIRATION_SECONDS)
            return {"ok": True, "status": 200, "error": None, "url": result["url"]}
        except ImageStorageError as exc:
            return {"ok": False, "status": 0, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc) or exc.__class__.__name__}

    def _parse_response(self, response: requests.Response) -> dict[str, object]:
        """解析 imgbb 响应，校验成功状态并提取图片地址。"""
        try:
            payload: dict[str, Any] = response.json()
        except Exception as exc:
            raise ImageStorageError("imgbb upload failed: invalid JSON response") from exc

        if response.status_code >= 400 or not payload.get("success"):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("code") or response.status_code)
            else:
                message = str(error or payload.get("message") or response.status_code)
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
