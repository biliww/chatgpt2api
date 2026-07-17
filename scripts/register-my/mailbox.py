"""邮箱适配层：直接复用项目既有架构 services.register.mail_provider。

本文件不做任何邮箱协议实现，只做薄封装，确保“浏览器注册机”与现有
Web 注册机使用同一套邮箱服务商逻辑（含 9 种 provider、配置格式、默认
tempmail_lol）。这样后续邮箱服务商的新增 / 修复只需改一处。

对外暴露：
    create_mailbox(mail_config, username=None) -> dict
    wait_for_code(mail_config, mailbox) -> str | None
其中 mail_config 的结构与 config-register.json 的 "mail" 字段完全一致。
"""
from __future__ import annotations

from typing import Any

# 复用本架构：直接导入既有邮箱模块
from services.register import mail_provider


def create_mailbox(mail_config: dict, username: str | None = None) -> dict[str, Any]:
    """创建一个临时邮箱，返回 {provider, address, token, ...}。

    默认行为由 mail_config["providers"] 中 enable=true 的条目决定；
    若只启用 tempmail_lol，则默认走 tempmail_lol（符合需求“默认 tempmail_lol”）。
    """
    return mail_provider.create_mailbox(mail_config, username)


def wait_for_code(mail_config: dict, mailbox: dict) -> str | None:
    """轮询邮箱直到收到 6 位验证码，返回验证码字符串或 None（超时）。"""
    return mail_provider.wait_for_code(mail_config, mailbox)


def list_enabled_providers(mail_config: dict) -> list[str]:
    """返回当前启用的 provider 类型列表，便于日志展示。"""
    out: list[str] = []
    for item in mail_config.get("providers", []) or []:
        if item.get("enable") and item.get("type"):
            out.append(str(item["type"]))
    return out
