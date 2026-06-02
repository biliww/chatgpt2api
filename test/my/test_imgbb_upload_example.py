from __future__ import annotations

import base64
import os
import unittest
from pathlib import Path
from typing import Any

from curl_cffi import CurlMime, requests


class ImgbbUploadExample:
    """演示 imgbb 图床上传流程，负责把图片 bytes 上传并返回图片直链。"""

    def __init__(self, api_key: str, expiration: int = 0) -> None:
        """初始化上传示例客户端，保存 API Key 和可选过期时间。"""
        self.api_key = api_key.strip()
        self.expiration = expiration
        self.endpoint = "https://api.imgbb.com/1/upload"

    def upload_bytes(self, image_data: bytes, name: str = "chatgpt2api-imgbb-test") -> str:
        """上传图片二进制数据到 imgbb，并返回响应中的图片访问地址。"""
        if not self.api_key:
            raise ValueError("IMGBB_API_KEY is required")
        if not image_data:
            raise ValueError("image_data is empty")
        if len(image_data) > 32 * 1024 * 1024:
            raise ValueError("imgbb image exceeds 32MB limit")

        params: dict[str, object] = {"key": self.api_key}
        if self.expiration:
            params["expiration"] = self.expiration

        multipart = CurlMime()
        multipart.addpart(name="image", filename=f"{name}.png", content_type="image/png", data=image_data)
        try:
            response = requests.post(
                self.endpoint,
                params=params,
                multipart=multipart,
                timeout=60,
            )
        finally:
            multipart.close()
        return self._extract_url(response)

    def upload_file(self, image_path: Path) -> str:
        """读取本地图片文件并上传到 imgbb，返回图片访问地址。"""
        return self.upload_bytes(image_path.read_bytes(), name=image_path.stem)

    def _extract_url(self, response: requests.Response) -> str:
        """解析 imgbb JSON 响应，提取可直接访问的图片 URL。"""
        payload: dict[str, Any] = response.json()
        if response.status_code >= 400 or not payload.get("success"):
            raise RuntimeError(f"imgbb upload failed: status={response.status_code}, body={payload}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"imgbb upload failed: missing data, body={payload}")

        url = str(data.get("url") or data.get("display_url") or data.get("url_viewer") or "").strip()
        if not url:
            raise RuntimeError(f"imgbb upload failed: missing url, body={payload}")
        return url


def tiny_png_bytes() -> bytes:
    """返回一张 1x1 PNG 图片，用于不依赖本地图片文件的上传测试。"""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )


class ImgbbUploadExampleTests(unittest.TestCase):
    """验证 imgbb 上传示例可以上传图片并拿到远程 URL。"""

    def test_upload_tiny_png_and_return_url(self) -> None:
        """使用环境变量中的 API Key 上传测试图片，并断言返回 imgbb 地址。"""
        api_key = os.getenv("IMGBB_API_KEY", "").strip()
        # api_key = "fee876c348ee3e74c6dca9f2d4281238";
        if not api_key:
            self.skipTest("请先设置环境变量 IMGBB_API_KEY")

        expiration = int(os.getenv("IMGBB_EXPIRATION", "600") or "600")
        image_path = os.getenv("IMGBB_TEST_IMAGE", "").strip()
        # image_path = '/Users/wangpenglong/projects/github/chatgpt2api/data/images/2026/06/02/1780382910_21c382c4eac5b15745165856a052ea95.png';
        client = ImgbbUploadExample(api_key, expiration=expiration)

        url = client.upload_file(Path(image_path)) if image_path else client.upload_bytes(tiny_png_bytes())

        print(f"imgbb url: {url}")
        self.assertTrue(url.startswith(("http://", "https://")))
        self.assertIn("ibb", url)


if __name__ == "__main__":
    unittest.main()
