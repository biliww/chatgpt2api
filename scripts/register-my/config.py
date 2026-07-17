"""配置加载：复用 config-register.json 的 mail/proxy/total/threads 结构，
并扩展一个 browser 段用于控制无头/有头、代理、UA、视口等。

配置来源（按优先级）：
    1. 命令行 --config 指定的独立 JSON 文件；
    2. 项目根的 config-register.json（Web 注册机同源配置）；
    3. 内置默认值。

这样浏览器注册机既能“独立运行”，也能“和 Web 注册机共用一套邮箱/代理配置”。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 60,
        "wait_interval": 3,
        "providers": [
            {
                "enable": True,
                "type": "tempmail_lol",
                "api_key": "",
            }
        ],
    },
    "proxy": "",
    "total": 1,
    "threads": 1,
    "browser": {
        "headless": True,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1280, "height": 800},
        "args": [],
        "executable_path": "",
        "timeout": 30000,
        "save_failure_screenshot": True,
        "screenshot_dir": "data/register-my-screenshots",
    },
    "output": {
        "write_pool": True,          # 注册成功是否写入本地号池
        "jsonl_path": "",            # 非空则额外写 JSONL 备份
        "remote_base_url": "",       # 非空则推送到远程 /api/accounts
        "remote_admin_key": "",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载并规范化配置。path 为空时尝试项目根 config-register.json。"""
    raw: dict[str, Any] = {}
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
    else:
        # 退回项目根 config-register.json
        root = Path(__file__).resolve().parents[2] / "config-register.json"
        if root.exists():
            raw = json.loads(root.read_text(encoding="utf-8"))
    cfg = _deep_merge(DEFAULTS, raw)

    # 兼容：browser.proxy 未显式设置时，继承顶层 proxy
    if not cfg["browser"].get("proxy"):
        cfg["browser"]["proxy"] = cfg.get("proxy", "")

    # 基本校验
    providers = cfg.get("mail", {}).get("providers", []) or []
    if not any(p.get("enable") for p in providers):
        raise ValueError("mail.providers 至少需要一个 enable=true 的邮箱服务商")
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    return cfg
