"""浏览器驱动封装：基于 Playwright（Chromium）实现无头/有头两种模式。

设计目标
--------
1. 复用项目既有邮箱架构（services.register.mail_provider）完成 OTP 验证，
   因此浏览器层只负责“在真实浏览器里走完 OpenAI 注册 UI”。
2. 支持无头（headless）与有头（headed）两种模式，通过配置或命令行切换。
3. 每个注册任务使用独立的浏览器上下文（独立 cookie / localStorage），
   避免账号之间相互串号。
4. 内置基础隐匿（抹掉 navigator.webdriver 痕迹、贴近真实 UA / 视口），
   降低被 Cloudflare / Turnstile 拦的概率。

依赖（一次性安装）：
    pip install playwright
    playwright install chromium        # 下载 Chromium 二进制

说明：playwright 采用惰性导入（在 launch() 时才 import），这样即使当前
环境尚未安装 playwright，本模块也能被 import（例如 `cli.py --help`），
只会在真正启动浏览器时抛出清晰的安装提示。
"""
from __future__ import annotations

import re
import time
from typing import Any


class BrowserError(RuntimeError):
    """浏览器驱动层统一异常。"""


# 贴近真实 Chrome 的 UA（与 services/register/openai_register.py 保持一致口径）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

# 基础隐匿启动参数
_STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]


class Browser:
    """对单个浏览器进程的轻封装，对外暴露注册流程需要的最小操作集。

    一个 Browser 实例对应“一个浏览器进程 + 一个默认上下文 + 一个页面”。
    并发场景下，建议每个注册任务各自 new 一个 Browser（线程数默认较小），
    如需更高并发可改造成“单浏览器 + 多上下文”，本类已预留 new_context 能力。
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
        viewport: dict[str, int] | None = None,
        args: list[str] | None = None,
        executable_path: str = "",
        timeout: int = 30000,
    ) -> None:
        self.headless = headless
        self.proxy = (proxy or "").strip()
        self.user_agent = user_agent
        self.viewport = viewport or {"width": 1280, "height": 800}
        self.extra_args = list(args or [])
        self.executable_path = executable_path
        self.timeout = timeout
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ── 生命周期 ───────────────────────────────────────────────
    def launch(self) -> "Browser":
        try:
            from playwright.sync_api import sync_playwright  # 惰性导入
        except ImportError as exc:  # pragma: no cover
            raise BrowserError(
                "未安装 playwright，请先执行：pip install playwright && playwright install chromium"
            ) from exc

        self._pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": [*_STEALTH_ARGS, *self.extra_args],
        }
        if self.executable_path:
            launch_kwargs["executable_path"] = self.executable_path
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        try:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception as exc:
            raise BrowserError(
                f"启动 Chromium 失败：{exc}。若提示缺少浏览器二进制，请执行 playwright install chromium"
            ) from exc
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            viewport=self.viewport,
            proxy={"server": self.proxy} if self.proxy else None,
        )
        self._page = self._context.new_page()
        # 抹掉 webdriver 痕迹，降低反爬识别
        self._page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        return self

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._pw = None

    # ── 导航 ───────────────────────────────────────────────────
    def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        if self._page is None:
            raise BrowserError("浏览器尚未 launch()")
        self._page.goto(url, wait_until=wait_until, timeout=self.timeout)

    @property
    def url(self) -> str:
        return str(self._page.url) if self._page else ""

    def wait_for_url(self, pattern: str, timeout: int | None = None) -> None:
        if self._page is None:
            raise BrowserError("浏览器尚未 launch()")
        self._page.wait_for_url(re.compile(pattern), timeout=timeout or self.timeout)

    # ── 元素交互 ───────────────────────────────────────────────
    def fill(self, selector: str, value: str, timeout: int | None = None) -> None:
        self._page.fill(selector, value, timeout=timeout or self.timeout)

    def click(self, selector: str, timeout: int | None = None) -> None:
        self._page.click(selector, timeout=timeout or self.timeout)

    def click_text(self, text: str, timeout: int | None = None) -> None:
        """按可见文本点击（更贴近人类操作，抗 DOM 结构微调）。"""
        self._page.get_by_text(text, exact=False).first.click(timeout=timeout or self.timeout)

    def click_by_regex(self, pattern: str, timeout: int | None = None, flags: int = re.IGNORECASE) -> None:
        """按正则（默认大小写不敏感）匹配可见文本后点击，抗文案大小写/空格差异。"""
        self._page.get_by_text(re.compile(pattern, flags), exact=False).first.click(timeout=timeout or self.timeout)

    def click_link_by_href(self, substr: str, timeout: int | None = None) -> None:
        """按链接 href 子串点击（作为文本匹配失败时的兜底，例如注册/登录入口）。"""
        self._page.locator(f"a[href*='{substr}']").first.click(timeout=timeout or self.timeout)

    def fill_by_label(self, label: str, value: str, timeout: int | None = None) -> None:
        """按表单 label 文本填写输入框。"""
        self._page.get_by_label(label, exact=False).fill(value, timeout=timeout or self.timeout)

    def wait_for_selector(self, selector: str, timeout: int | None = None, state: str = "visible") -> None:
        self._page.wait_for_selector(selector, state=state, timeout=timeout or self.timeout)

    def get_text(self, selector: str) -> str:
        return str(self._page.text_content(selector) or "").strip()

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        return self._page.evaluate(expression, arg)

    def get_cookies(self) -> list[dict]:
        if self._context is None:
            return []
        return self._context.cookies()

    def screenshot(self, path: str) -> None:
        if self._page is not None:
            self._page.screenshot(path=path)

    def find_input_by_type(self, input_type: str):
        """返回匹配 type 的输入框定位器，便于 OTP / 密码等场景。"""
        return self._page.locator(f"input[type='{input_type}']").first

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)
