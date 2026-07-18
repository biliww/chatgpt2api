"""浏览器注册编排：在真实 Chromium 中走完 OpenAI 注册 UI，并使用复用架构的
邮箱完成 OTP 验证，最后提取登录态（token / cookie）产出账号。

流程概览
--------
1. 通过复用架构创建临时邮箱（默认 tempmail_lol）；
2. 启动浏览器（无头/有头），打开注册入口；
3. 填写邮箱 -> 继续 -> 跳转到 OTP 验证页；
4. 轮询邮箱拿到 6 位验证码 -> 填验证码 -> 继续；
5. 设置密码 -> 继续；
6. 填写姓名 + 生日（about-you）-> 继续；
7. 等待落地到 chatgpt.com；
8. 通过 /api/auth/session + cookie 提取 token，产出账号。

⚠️ OpenAI 注册页 DOM / 文案随版本变化较快，选择器采用
“语义定位 + 多候选 + 失败截图 + DEBUG 级 DOM 自检”策略。
把配置 log.level 设为 DEBUG 可打印每一步的页面 URL / 输入框结构 /
即将点击的按钮，便于无图调试。
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
from logger import make_logger, Logger


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

    def __init__(self, cfg: dict, index: int, log: Logger | None = None) -> None:
        self.cfg = cfg
        self.index = index
        self.log: Logger = log or make_logger()
        self.mail_config = cfg["mail"]
        self.browser_cfg = cfg["browser"]
        self.browser: Browser | None = None
        # 步骤计时：_t0=任务起点，_t_last=上一步时刻（单调时钟，避免系统时间回拨干扰）
        self._t0: float = 0.0
        self._t_last: float = 0.0
        # 后续步骤「已完成」标志：避免提交在途/重定向过渡期内被自适应循环
        # 误判为「还在本页」而重复填写（曾导致 about-you 提交后空等 ~180s）。
        self._about_you_done: bool = False
        self._password_done: bool = False

    # ── 步骤计时 ───────────────────────────────────────────────
    def _mark_step(self, step: str) -> None:
        """打印累计耗时与本步耗时的进度标记。

        日志本身已带 [MM-DD HH:MM:SS] 时间戳；此处再补「累计 / 本步」耗时，
        便于定位注册慢在哪一环（例如 about-you 提交后到落地 chatgpt.com 的间隔）。
        """
        now = time.monotonic()
        if not self._t0:
            self._t0 = now
            self._t_last = now
        total = now - self._t0
        delta = now - self._t_last
        self._t_last = now
        self.log.info(
            f"[任务{self.index}] ▶ {step} | 累计 {total:.1f}s | 本步 {delta:.1f}s"
        )

    # ── 对外主流程 ─────────────────────────────────────────────
    def register(self) -> dict:
        self._mark_step("任务开始")
        mailbox = create_mailbox(self.mail_config)
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        label = str(mailbox.get("label") or mailbox.get("provider") or "")
        self.log.info(f"[任务{self.index}] 邮箱创建完成[{label}]: {email}")
        self._mark_step("创建邮箱完成")

        password = _random_password()
        first, last = _random_name()
        birthdate = _random_birthdate()

        started = time.time()
        try:
            self.browser = self._make_browser()
            self.browser.launch()
            self.log.debug(f"[任务{self.index}] 浏览器已启动 headless={self.browser_cfg.get('headless')} proxy={self.browser_cfg.get('proxy') or '无'}")
            self._mark_step("浏览器启动")
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
            self.log.info(f"[任务{self.index}] 浏览器注册成功，耗时{cost:.1f}s: {email}", "green")
            return result
        except Exception as exc:
            cost = time.time() - started
            self._save_failure_screenshot()
            self.log.error(f"[任务{self.index}] 浏览器注册失败（耗时{cost:.1f}s）: {exc}", "red")
            # 失败时打印当前页面结构，便于无图定位
            self._dump_page_state("failure")
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
            d = Path(self.browser_cfg.get("screenshot_dir") or "scripts/register-my/output/screenshots")
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"fail-task{self.index}-{uuid.uuid4().hex[:6]}.png"
            self.browser.screenshot(str(path))
            self.log.warn(f"[任务{self.index}] 已保存失败截图: {path}")
        except Exception:
            pass

    # ── 详细 DEBUG：打印页面结构（无图调试用）────────────────
    def _dump_page_state(self, label: str) -> None:
        b = self.browser
        if b is None:
            return
        try:
            data = b.evaluate(
                """(function(){
                    function vis(el){ return el.offsetParent !== null && el.getClientRects().length > 0; }
                    var inputs = Array.prototype.slice.call(document.querySelectorAll('input')).map(function(e,i){
                        return {i:i, type:e.type, name:e.name||'', id:e.id||'',
                                ph:(e.placeholder||'').slice(0,30),
                                aria_hidden:e.getAttribute('aria-hidden')||'',
                                visible:vis(e)};
                    });
                    var btns = Array.prototype.slice.call(document.querySelectorAll('button,a,[role=button]')).map(function(e,i){
                        return {i:i, tag:e.tagName, text:(e.innerText||'').replace(/\\s+/g,' ').trim().slice(0,40), visible:vis(e)};
                    }).filter(function(x){return x.visible;});
                    return {url:location.href, title:document.title, inputs:inputs, buttons:btns};
                })()"""
            )
            self.log.debug(f"[DOM:{label}] url={data.get('url')}")
            self.log.debug(f"[DOM:{label}] title={data.get('title')!r}")
            for inp in data.get("inputs", []):
                self.log.debug(
                    f"[DOM:{label}] input#{inp['i']} type={inp['type']} name={inp['name']!r} "
                    f"id={inp['id']!r} ph={inp['ph']!r} aria-hidden={inp['aria_hidden']} visible={inp['visible']}"
                )
            for btn in data.get("buttons", []):
                self.log.debug(f"[DOM:{label}] btn <{btn['tag']}> text={btn['text']!r}")
        except Exception as e:
            self.log.debug(f"[DOM:{label}] dump failed: {e}")

    # ── 注册 UI 流程 ──────────────────────────────────────────
    # 当前 ChatGPT 注册流程（实测）：
    #   chatgpt.com → Sign up for free → 弹出 Log in or sign up 模态框
    #   → 输入邮箱 → Continue → auth.openai.com/email-verification（OTP 验证）
    #   → 填写 6 位验证码 → Continue → 设置密码 → about-you → 落地 chatgpt.com
    def _signup(self, email: str, password: str, first: str, last: str, birthdate: str, mailbox: dict) -> None:
        b = self.browser
        assert b is not None
        self.log.info(f"[任务{self.index}] 打开注册入口 (chatgpt.com)")
        b.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        self._dump_page_state("home")

        # ── Step 1: 点击注册入口 ──
        self._click_signup_entry()
        self._mark_step("进入邮箱输入页")
        self._dump_page_state("after-signup-entry")

        # ── Step 2: 填写邮箱并提交 ──
        self.log.info(f"[任务{self.index}] 填写邮箱: {email}")
        b.wait_for_selector("input[type='email']", timeout=15000)
        b.fill("input[type='email']", email)
        self._dump_page_state("email-filled")
        self._click_form_continue()
        self.log.debug(f"[任务{self.index}] 已提交邮箱，等待 OTP 验证页面...")
        b.wait_for_url(r"(email-verification|check.your.inbox|auth\.openai\.com)", timeout=30000)
        b.wait(2)
        self._mark_step("提交邮箱→到达OTP页")
        self._dump_page_state("otp-page")

        # ── Step 3: OTP 邮箱验证（在 password 之前！）──
        self.log.info(f"[任务{self.index}] 轮询邮箱获取验证码")
        code = wait_for_code(self.mail_config, mailbox)
        if not code:
            raise RuntimeError("等待注册验证码超时")
        self.log.info(f"[任务{self.index}] 收到验证码: {code}")
        self._mark_step("收到验证码")
        self._fill_otp(code)
        self._dump_page_state("otp-filled")
        self._click_form_continue()
        b.wait(3)
        self._mark_step("提交验证码")
        self._dump_page_state("after-otp")

        # ── Step 4+: OTP 之后的步骤顺序不固定（about-you / password 先后不定）──
        # 采用自适应循环：识别当前页面类型并填表，直到落地 chatgpt.com。
        self._complete_remaining_steps(first, last, birthdate, password)
        self._mark_step("后续步骤完成")

        # ── 最后：等待落地到 chatgpt.com（非 auth 页）──
        self.log.info(f"[任务{self.index}] 等待注册完成落地")
        try:
            b.wait_for_url(r"chatgpt\.com/(?!auth|login|email-verification|about-you)", timeout=60000)
        except Exception:
            self.log.warn(f"[任务{self.index}] 60s 内未落地 chatgpt.com，当前 url={b.url}")
            self._dump_page_state("landing-timeout")
        b.wait(3)  # 让会话 cookie 稳定写入
        self._mark_step("会话cookie稳定，注册流程结束")

    def _page_has(self, selector: str) -> bool:
        b = self.browser
        assert b is not None
        try:
            return b._page.locator(selector).count() > 0
        except Exception:
            return False

    def _is_error_page(self) -> bool:
        """识别 OpenAI 的临时错误页（"Oops, an error occurred!" + 仅 Try again）。

        OTP 提交后 OpenAI 偶发返回该页（服务端瞬时故障 / 风控），页面无任何表单
        输入框、只剩 "Try again"。需专门识别，避免掉进"未识别页面"兜底分支里
        用 _click_form_continue 挨个试选择器空等 ~40s。
        """
        b = self.browser
        assert b is not None
        try:
            title = (b._page.title() or "").lower()
        except Exception:
            title = ""
        if "error occurred" in title or "oops" in title:
            return True
        try:
            has_try_again = b._page.get_by_text("Try again", exact=False).count() > 0
        except Exception:
            has_try_again = False
        if has_try_again and not self._page_has(
            "input[name='code'], input[name='name'], input[type='password'], input[type='email']"
        ):
            return True
        return False

    def _about_you_filled(self) -> bool:
        """about-you 的姓名框是否已填好（用于避免提交在途时重复填写导致竞态）。"""
        b = self.browser
        assert b is not None
        try:
            el = b._page.locator("input[name='name'], input[placeholder*='Full name' i]").first
            if el.count() and (el.input_value() or "").strip():
                return True
        except Exception:
            pass
        return False

    def _wait_until_gone(self, selector: str, timeout: float = 30000) -> bool:
        """轮询直到给定选择器从页面消失（说明上一步提交已生效、页面已跳转）。

        用于在点完 Continue/提交后，等待页面真正离开当前步骤，避免“提交在途、
        页面尚未跳走”的窗口期内被自适应循环误判为“还在本页”而重复填写。
        """
        b = self.browser
        assert b is not None
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            if not self._page_has(selector):
                return True
            b.wait(1)
        return False

    def _complete_remaining_steps(self, first: str, last: str, birthdate: str, password: str) -> None:
        """OTP 之后自适应的后续步骤循环：

        当前 ChatGPT 注册在 OTP 之后会依次出现 about-you（姓名+年龄）与
        创建密码两个页面，但先后顺序不定，且可能夹带其他问卷页。这里按
        “识别页面类型 -> 填表 -> 点 Continue”循环，直到落地 chatgpt.com。
        """
        b = self.browser
        assert b is not None
        import re as _re

        for step in range(8):
            url = b.url
            self.log.debug(f"[任务{self.index}] 后续步骤[{step}] url={url}")
            self._dump_page_state(f"post-otp-{step}")

            # 实时重读当前 URL（重定向过渡期 url 会滞后，必须用最新值判定落地）
            live_url = b.url or ""

            # 已落地 chatgpt.com（非 auth 子页）
            if _re.search(r"chatgpt\.com/(?!auth|login|email-verification|about-you)", live_url):
                self.log.info(f"[任务{self.index}] 已落地 chatgpt.com")
                self._mark_step("已落地 chatgpt.com")
                return

            # about-you 页面：含 Full name / Age（"How old are you?"）
            # 关键：用「已完成」标志拦截重复填写——一旦提交过 about-you 就不再进该分支，
            # 否则重定向过渡期 url 仍停留在 about-you 时会被误判为「还在本页」而空等。
            if not self._about_you_done and (
                "about-you" in live_url
                or self._page_has("input[name='name'], input[placeholder*='Full name' i]")
                or self._page_has("input[name='age'], input[placeholder*='Age' i]")
            ):
                self.log.info(f"[任务{self.index}] 填写 about-you（姓名 + 年龄）")
                self._mark_step("开始填写 about-you")
                self._fill_about_you(first, last, birthdate)
                self._dump_page_state("about-you-filled")
                self._click_form_continue()
                self._about_you_done = True
                self._mark_step("提交 about-you（等待跳转）")
                # 等待本次提交生效、about-you 输入消失（页面已跳转）再进入下一轮，
                # 避免“提交在途、页面未跳走”窗口期内被误判为仍在本页而重复填写。
                self._wait_until_gone("input[name='name'], input[placeholder*='Full name' i]", timeout=30000)
                continue

            # 创建密码页面
            if not self._password_done and self._page_has("input[type='password']"):
                self.log.info(f"[任务{self.index}] 填写密码")
                self._mark_step("填写密码")
                self._fill_password(password)
                self._dump_page_state("password-filled")
                self._click_form_continue()
                self._password_done = True
                self._mark_step("提交密码")
                self._wait_until_gone("input[type='password']", timeout=30000)
                continue

            # 未知页面：尝试点 Continue 推进（可能是问卷/欢迎页）
            self.log.warn(f"[任务{self.index}] 遇到未识别页面，尝试点击 Continue 推进: url={live_url}")
            try:
                self._click_form_continue()
            except Exception:
                self.log.error(f"[任务{self.index}] 未识别页面且无法推进，停止后续步骤: url={live_url}")
                self._dump_page_state("stuck")
                return
            b.wait(3)

    def _click_signup_entry(self) -> None:
        """进入邮箱输入步骤。注册入口有两类形态，需自适应：

        情形 A：首页 chatgpt.com 渲染的是“营销首页”，需点击 “Sign up for free”
                触发登录/注册模态框（内含 email 输入框）；
        情形 B：有时直接进入 “Get started | ChatGPT” 登录/注册页
                （chatgpt.com/auth/login），页面已自带 email 输入框。
        两种情形最终都出现 email 输入框，本方法确保它出现即可。
        """
        b = self.browser
        assert b is not None
        b.wait(3)  # 等页面 JS 完全渲染

        # 情形 B：当前页已直接含 email 输入框，无需点击“Sign up for free”
        if self._page_has("input[type='email']"):
            self.log.debug(f"[任务{self.index}] 当前页已含 email 输入框（auth 登录/注册入口），直接进入邮箱填写")
            return

        # 情形 A：首页点击 Sign up for free（多策略兜底）
        clicked = False
        # 1) 精确文案 —— 用 JS .click() 绕过 Playwright actionability 检查
        try:
            el = b._page.get_by_text("Sign up for free", exact=False).first
            el.wait_for(state="visible", timeout=20000)
            el.evaluate("el => el.click()")
            clicked = True
            self.log.debug(f"[任务{self.index}] 已点击 Sign up for free（JS click）")
        except Exception as e:
            self.log.debug(f"[任务{self.index}] 精确文案点击失败: {e}")
        # 2) 可交互元素 + 正则
        if not clicked:
            import re as _re
            try:
                loc = b._page.locator("button, a, [role='button']").filter(
                    has_text=_re.compile(r"sign\s*up|注册", _re.IGNORECASE)
                ).first
                loc.wait_for(state="visible", timeout=20000)
                loc.evaluate("el => el.click()")
                clicked = True
                self.log.debug(f"[任务{self.index}] 已点击注册入口（正则匹配）")
            except Exception as e:
                self.log.debug(f"[任务{self.index}] 正则匹配点击失败: {e}")
        # 3) href 兜底
        if not clicked:
            for href in ("signup", "auth/login", "auth/signup", "register"):
                try:
                    b.click_link_by_href(href, timeout=15000)
                    clicked = True
                    self.log.debug(f"[任务{self.index}] 已点击注册入口（href={href}）")
                    break
                except Exception:
                    continue
        # 4) 直接打开 auth/login 入口兜底
        if not clicked:
            try:
                b.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded")
                b.wait(3)
                if self._page_has("input[type='email']"):
                    self.log.debug(f"[任务{self.index}] 已通过直接打开 auth/login 进入")
                    return
            except Exception as e:
                self.log.debug(f"[任务{self.index}] 直接打开 auth/login 失败: {e}")
        if not clicked:
            raise RuntimeError("找不到注册入口（Sign up for free / Sign up / 注册）")
        b.wait(3)
        b.wait_for_selector("input[type='email']", timeout=45000)

    def _click_form_continue(self) -> None:
        """点击表单的 Continue/Next 按钮，优先排除社交登录（Google/Microsoft/Apple 等）。"""
        b = self.browser
        assert b is not None
        # 1) 优先：表单 submit 按钮
        try:
            b.click("button[type='submit']", timeout=10000)
            self.log.debug(f"[任务{self.index}] 已点击 button[type=submit]")
            return
        except Exception:
            pass
        # 2) 精确 "Continue" / "Next" 文案（排除社交登录）
        for text in ("Continue", "Next"):
            try:
                btns = b._page.get_by_text(text, exact=False)
                count = btns.count()
                for i in range(count):
                    txt = (btns.nth(i).inner_text() or "").strip()
                    if any(s in txt.lower() for s in ("google", "microsoft", "apple", "github", "with ")):
                        continue
                    btns.nth(i).click(timeout=5000)
                    self.log.debug(f"[任务{self.index}] 已点击按钮文案={txt!r}")
                    return
            except Exception:
                pass
        # 3) 兜底
        try:
            b._page.get_by_text("Continue", exact=False).first.click(timeout=10000)
        except Exception:
            try:
                b.click("button[type='submit']", timeout=10000)
            except Exception:
                b._page.get_by_text("继续", exact=False).first.click(timeout=10000)

    def _fill_otp(self, code: str) -> None:
        """填写 OTP 验证码。OTP 输入框可能是 inputmode=numeric / name=code / id 含 code / label Code。"""
        b = self.browser
        assert b is not None
        # 等待 OTP 输入框出现
        try:
            b._page.locator("input[inputmode='numeric'], input[name='code'], input[id*='code' i], #code-input").first.wait_for(
                state="attached", timeout=15000
            )
        except Exception:
            pass
        locators = [
            "input[inputmode='numeric']",
            "input[name='code']",
            "input[id*='code' i]",
            "#code-input",
        ]
        target = None
        for sel in locators:
            try:
                el = b._page.locator(sel).first
                if el.count() and el.is_visible():
                    target = el
                    break
            except Exception:
                continue
        if target is None:
            try:
                target = b._page.get_by_label("Code", exact=False).first
            except Exception:
                pass
        if target is None:
            self._dump_page_state("otp-not-found")
            raise RuntimeError("找不到 OTP 验证码输入框")
        target.fill(code)
        self.log.debug(f"[任务{self.index}] 已填入验证码（已隐藏）")

    def _fill_password(self, password: str) -> None:
        """填写密码。等待可见的 password input（排除 honeypot hiddenPassword）。"""
        b = self.browser
        assert b is not None
        # 等任意 password input 出现
        try:
            b._page.locator("input[type='password']").first.wait_for(state="attached", timeout=20000)
        except Exception:
            pass
        # 选第一个可见的（排除 honeypot）
        pw = b._page.locator("input[type='password']")
        target = None
        for i in range(pw.count()):
            inp = pw.nth(i)
            try:
                if (inp.get_attribute("name") or "") == "hiddenPassword":
                    continue
                if (inp.get_attribute("aria-hidden") or "") == "true":
                    continue
                if inp.is_visible():
                    target = inp
                    break
            except Exception:
                continue
        if target is None:
            target = pw.first if pw.count() else None
        if target is None:
            self._dump_page_state("password-not-found")
            raise RuntimeError("找不到密码输入框")
        target.fill(password)
        self.log.debug(f"[任务{self.index}] 已填入密码（已隐藏）")

    def _fill_about_you(self, first: str, last: str, birthdate: str) -> None:
        b = self.browser
        assert b is not None
        full = f"{first} {last}"
        # 当前 about-you 页面（"How old are you?"）：Full name + Age
        # 兼容旧版：First name / Last name + 生日
        # 统一短超时（8s）：即便被误调用到非 about-you 页面，也快速失败而非卡死等待。
        TO = 8000
        # 1) 姓名
        try:
            b.fill("input[name='name'], input[placeholder*='Full name' i], input[id*='name' i]", full, timeout=TO)
            self.log.debug(f"[任务{self.index}] 已填写 Full name: {full}")
        except Exception:
            try:
                b.fill_by_label("First name", first, timeout=TO)
                b.fill_by_label("Last name", last, timeout=TO)
            except Exception:
                try:
                    b.fill("input[name='firstName'], input[name='first']", first, timeout=TO)
                    b.fill("input[name='lastName'], input[name='last']", last, timeout=TO)
                except Exception:
                    self.log.warn(f"[任务{self.index}] 姓名填写失败，请检查 about-you 选择器")
        # 2) 年龄 / 生日
        try:
            age = str(random.randint(20, 34))
            b.fill("input[name='age'], input[placeholder*='Age' i]", age, timeout=TO)
            self.log.debug(f"[任务{self.index}] 已填写 Age: {age}")
        except Exception:
            try:
                y, m, d = birthdate.split("-")
                b.fill("input[type='date']", birthdate, timeout=TO)
            except Exception:
                try:
                    b.fill("select[name='birthdateMonth'], select[name='month']", m, timeout=TO)
                    b.fill("select[name='birthdateDay'], select[name='day']", d, timeout=TO)
                    b.fill("select[name='birthdateYear'], select[name='year']", y, timeout=TO)
                except Exception:
                    self.log.warn(f"[任务{self.index}] 年龄/生日填写跳过（页面结构不匹配）")

    # ── 登录态提取 ─────────────────────────────────────────────
    def _collect_tokens(self) -> dict[str, str]:
        b = self.browser
        assert b is not None
        tokens: dict[str, str] = {}
        # 1) /api/auth/session 通常返回 accessToken
        for attempt in range(3):
            try:
                session = b.evaluate(
                    "async () => { const r = await fetch('/api/auth/session'); return r.json(); }"
                )
                if isinstance(session, dict):
                    tokens["access_token"] = str(session.get("accessToken") or "").strip()
                    tokens["id_token"] = str(session.get("idToken") or "").strip()
                    if tokens["access_token"]:
                        break
            except Exception as exc:
                self.log.debug(f"[任务{self.index}] 读取 /api/auth/session 失败(第{attempt+1}次): {exc}")
            b.wait(2)
        if not tokens.get("access_token"):
            self.log.warn(f"[任务{self.index}] 未能从 /api/auth/session 取到 access_token（可能页面未完全落地）")
        # 2) cookie 中提取 refresh_token / session_token
        for c in b.get_cookies():
            name = str(c.get("name") or "")
            value = str(c.get("value") or "")
            if name == "refresh_token":
                tokens["refresh_token"] = value
            elif name in ("__Secure-next-auth.session-token", "session_token"):
                tokens["session_token"] = value
        return tokens


def run_one(cfg: dict, index: int, log: Logger | None = None) -> dict:
    """线程池单任务入口：返回 {ok, index, result/error}。"""
    registrar = BrowserRegistrar(cfg, index, log)
    try:
        result = registrar.register()
        return {"ok": True, "index": index, "result": result}
    except Exception as exc:
        return {"ok": False, "index": index, "error": str(exc)}


# 便于外部直接 import 后调用
__all__ = ["BrowserRegistrar", "run_one", "make_logger"]
