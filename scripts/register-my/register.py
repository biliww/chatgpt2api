"""浏览器注册编排：在真实 Chromium 中走完 OpenAI 注册 UI，并使用复用架构的
邮箱完成 OTP 验证，最后提取登录态（token / cookie）产出账号。

流程概览
--------
1. 通过复用架构创建临时邮箱（默认 tempmail_lol）；
2. 启动浏览器（无头/有头），打开注册入口；
3. 填写邮箱 -> 继续 -> 填写密码 -> 继续；
4. 触发验证码后，用同一套邮箱逻辑轮询 OTP；
5. 回到浏览器填写验证码；
6. 填写姓名 + 生日（about-you）；
7. 等待落地到 chatgpt.com；
8. 通过 /api/auth/session + cookie 提取 token，产出账号。

⚠️ 注意：OpenAI 注册页 DOM / 文案随版本变化较快，下面选择器采用
“语义定位 + 多候选 + 失败截图”的策略，首次上线前需在目标环境用
有头模式（--headed）跑通一次，必要时按实际页面调整选择器。
"""
from __future__ import annotations

import json
import random
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from browser import Browser, BrowserError
from mailbox import create_mailbox, list_enabled_providers, wait_for_code


_log_lock = threading.Lock()


def make_logger(sink: Callable[[str, str], None] | None = None) -> Callable[[str, str], None]:
    def log(text: str, color: str = "") -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
        with _log_lock:
            if sink:
                sink(line, color)
            print(line)
    return log


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        random.choice(string.ascii_uppercase)
        + random.choice(string.ascii_lowercase)
        + random.choice(string.digits)
        + random.choice("!@#$%")
        + "".join(random.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


class BrowserRegistrar:
    """单个浏览器注册任务。一个实例对应一个邮箱 + 一个浏览器上下文。"""

    def __init__(self, cfg: dict, index: int, log: Callable[[str, str], None] | None = None) -> None:
        self.cfg = cfg
        self.index = index
        self.log = log or make_logger()
        self.mail_config = cfg["mail"]
        self.browser_cfg = cfg["browser"]
        self.browser: Browser | None = None

    # ── 对外主流程 ─────────────────────────────────────────────
    def register(self) -> dict:
        mailbox = create_mailbox(self.mail_config)
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or mailbox.get("provider") or "")
        self.log(f"[任务{self.index}] 邮箱创建完成[{label}]: {email}")

        password = _random_password()
        first, last = _random_name()
        birthdate = _random_birthdate()

        started = time.time()
        try:
            self.browser = self._make_browser()
            self.browser.launch()
            self._signup(email, password, first, last, birthdate, mailbox)
            tokens = self._collect_tokens()
            cost = time.time() - started
            result = {
                "email": email,
                "password": password,
                "access_token": str(tokens.get("access_token") or "").strip(),
                "refresh_token": str(tokens.get("refresh_token") or "").strip(),
                "id_token": str(tokens.get("id_token") or "").strip(),
                "session_token": str(tokens.get("session_token") or "").strip(),
                "source_type": "browser",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.log(f"[任务{self.index}] 浏览器注册成功，耗时{cost:.1f}s: {email}", "green")
            return result
        except Exception as exc:
            cost = time.time() - started
            self._save_failure_screenshot()
            self.log(f"[任务{self.index}] 浏览器注册失败（耗时{cost:.1f}s）: {exc}", "red")
            raise
        finally:
            if self.browser:
                self.browser.close()

    # ── 浏览器构建 ─────────────────────────────────────────────
    def _make_browser(self) -> Browser:
        return Browser(
            headless=bool(self.browser_cfg.get("headless", True)),
            proxy=str(self.browser_cfg.get("proxy") or self.cfg.get("proxy") or "").strip(),
            user_agent=str(self.browser_cfg.get("user_agent") or ""),
            viewport=self.browser_cfg.get("viewport"),
            args=self.browser_cfg.get("args") or [],
            executable_path=str(self.browser_cfg.get("executable_path") or ""),
            timeout=int(self.browser_cfg.get("timeout") or 30000),
        )

    def _save_failure_screenshot(self) -> None:
        if not self.browser_cfg.get("save_failure_screenshot"):
            return
        try:
            d = Path(self.browser_cfg.get("screenshot_dir") or "data/register-my-screenshots")
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"fail-task{self.index}-{uuid.uuid4().hex[:6]}.png"
            self.browser.screenshot(str(path))
            self.log(f"[任务{self.index}] 已保存失败截图: {path}", "yellow")
        except Exception:
            pass

    # ── 注册 UI 流程（选择器需按实际页面校准）─────────────────
    def _signup(self, email: str, password: str, first: str, last: str, birthdate: str, mailbox: dict) -> None:
        b = self.browser
        assert b is not None
        self.log(f"[任务{self.index}] 打开注册入口")
        b.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        # 点击“Sign up”（兼容中文“注册”）
        try:
            b.click_text("Sign up", timeout=8000)
        except Exception:
            b.click_text("注册", timeout=8000)

        # 1) 邮箱
        self.log(f"[任务{self.index}] 填写邮箱")
        b.wait_for_selector("input[type='email']", timeout=15000)
        b.fill("input[type='email']", email)
        self._click_continue()

        # 2) 密码
        self.log(f"[任务{self.index}] 填写密码")
        b.wait_for_selector("input[type='password']", timeout=15000)
        b.fill("input[type='password']", password)
        self._click_continue()

        # 3) 等待并填入邮箱验证码（浏览器中可能提示去收件箱，这里回到邮件轮询）
        self.log(f"[任务{self.index}] 等待邮箱验证码")
        code = wait_for_code(self.mail_config, mailbox)
        if not code:
            raise RuntimeError("等待注册验证码超时")
        self.log(f"[任务{self.index}] 收到验证码: {code}")
        b.wait_for_selector("input[inputmode='numeric'], input[name='code'], input[type='text']", timeout=15000)
        b.fill("input[inputmode='numeric'], input[name='code'], input[type='text']", code)
        self._click_continue()

        # 4) 姓名 + 生日（about-you）
        self.log(f"[任务{self.index}] 填写姓名与生日")
        self._fill_about_you(first, last, birthdate)
        self._click_continue()

        # 5) 等待落地到 chatgpt.com（非 auth 页）
        self.log(f"[任务{self.index}] 等待注册完成落地")
        b.wait_for_url(r"chatgpt\.com/(?!auth|login)", timeout=60000)
        b.wait(3)  # 让会话 cookie 稳定写入

    def _click_continue(self) -> None:
        b = self.browser
        assert b is not None
        try:
            b.click_text("Continue", timeout=5000)
        except Exception:
            try:
                b.click("button[type='submit']", timeout=5000)
            except Exception:
                b.click_text("继续", timeout=5000)

    def _fill_about_you(self, first: str, last: str, birthdate: str) -> None:
        b = self.browser
        assert b is not None
        # 姓名：优先按 label，其次按 name 属性
        try:
            b.fill_by_label("First name", first)
        except Exception:
            b.fill("input[name='firstName'], input[name='first']", first)
        try:
            b.fill_by_label("Last name", last)
        except Exception:
            b.fill("input[name='lastName'], input[name='last']", last)
        # 生日：优先 date input，否则尝试月/日/年 select
        try:
            b.fill("input[type='date']", birthdate)
        except Exception:
            try:
                y, m, d = birthdate.split("-")
                b.fill("select[name='birthdateMonth'], select[name='month']", m)
                b.fill("select[name='birthdateDay'], select[name='day']", d)
                b.fill("select[name='birthdateYear'], select[name='year']", y)
            except Exception:
                self.log(f"[任务{self.index}] 生日填写跳过（页面结构不匹配），请检查 about-you 选择器", "yellow")

    # ── 登录态提取 ─────────────────────────────────────────────
    def _collect_tokens(self) -> dict[str, str]:
        b = self.browser
        assert b is not None
        tokens: dict[str, str] = {}
        # 1) /api/auth/session 通常返回 accessToken
        try:
            session = b.evaluate(
                "async () => { const r = await fetch('/api/auth/session'); return r.json(); }"
            )
            if isinstance(session, dict):
                tokens["access_token"] = str(session.get("accessToken") or "").strip()
                tokens["id_token"] = str(session.get("idToken") or "").strip()
        except Exception as exc:
            self.log(f"[任务{self.index}] 读取 /api/auth/session 失败: {exc}", "yellow")
        # 2) cookie 中提取 refresh_token / session_token
        for c in b.get_cookies():
            name = str(c.get("name") or "")
            value = str(c.get("value") or "")
            if name == "refresh_token":
                tokens["refresh_token"] = value
            elif name in ("__Secure-next-auth.session-token", "session_token"):
                tokens["session_token"] = value
        return tokens


def run_one(cfg: dict, index: int, log: Callable[[str, str], None] | None = None) -> dict:
    """线程池单任务入口：返回 {ok, index, result/error}。"""
    registrar = BrowserRegistrar(cfg, index, log)
    try:
        result = registrar.register()
        return {"ok": True, "index": index, "result": result}
    except Exception as exc:
        return {"ok": False, "index": index, "error": str(exc)}


# 便于外部直接 import 后调用
__all__ = ["BrowserRegistrar", "run_one", "make_logger"]
