"""可配置级别的日志模块。

支持 INFO / DEBUG / ERROR / WARN 四级，级别由配置 ``log.level`` 控制（默认 INFO）。
调试期把 level 设为 DEBUG 可打印“每一步的页面 URL / 输入框结构 / 即将点击的按钮”等详细信息。

特性
----
- 控制台彩色输出（DEBUG=蓝、WARN=黄、ERROR=红、INFO=默认）；
- 可选写入日志文件（log.to_file=true + log.file_path）；
- 线程安全（多任务并发打印不会错行）；
- 同时兼容旧接口 ``log(text, color)``（等价于 info）与分级接口 ``log.info/debug/warn/error``。
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import TextIO

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "NONE": 100}
_COLORS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}
_RESET = "\033[0m"

_lock = threading.Lock()


class Logger:
    def __init__(
        self,
        name: str = "register-my",
        level: str = "INFO",
        to_console: bool = True,
        to_file: bool = False,
        file_path: str = "",
    ) -> None:
        self.name = name
        self.level = _LEVELS.get(str(level or "INFO").upper(), 20)
        self.to_console = to_console
        self.to_file = to_file
        self._fh: TextIO | None = None
        if to_file and file_path:
            try:
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(file_path, "a", encoding="utf-8")
            except Exception:
                self._fh = None

    # ── 内部发射 ───────────────────────────────────────────────
    def _emit(self, lvl: str, text: str, color: str) -> None:
        if _LEVELS.get(lvl, 20) < self.level:
            return
        ts = datetime.now().strftime("%m-%d %H:%M:%S")
        line = f"[{ts}] [{lvl}] {text}"
        with _lock:
            if self.to_console:
                c = _COLORS.get(color, "")
                out = f"{c}{line}{_RESET}" if c else line
                print(out, flush=True)
            if self._fh:
                try:
                    self._fh.write(line + "\n")
                    self._fh.flush()
                except Exception:
                    pass

    # ── 分级接口 ───────────────────────────────────────────────
    def info(self, text: str, color: str = "") -> None:
        self._emit("INFO", text, color)

    def debug(self, text: str, color: str = "blue") -> None:
        self._emit("DEBUG", text, color)

    def warn(self, text: str, color: str = "yellow") -> None:
        self._emit("WARN", text, color)

    def error(self, text: str, color: str = "red") -> None:
        self._emit("ERROR", text, color)

    # ── 兼容旧接口：log(text, color) 等价于 info ──────────────
    # 同时支持把 Logger 实例当函数调用：log(text, color)
    def log(self, text: str, color: str = "") -> None:
        self.info(text, color)

    def __call__(self, text: str, color: str = "") -> None:
        self.info(text, color)

    def close(self) -> None:
        with _lock:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None


def make_logger(cfg: dict | None = None) -> Logger:
    """根据配置生成 Logger。配置段：

    log:
      level: DEBUG | INFO | WARN | ERROR   # 默认 INFO
      to_console: true
      to_file: false
      file_path: "scripts/register-my/output/register.log"
    """
    cfg = cfg or {}
    log_cfg = cfg.get("log", {}) or {}
    return Logger(
        name="register-my",
        level=str(log_cfg.get("level") or "INFO").upper(),
        to_console=bool(log_cfg.get("to_console", True)),
        to_file=bool(log_cfg.get("to_file", False)),
        file_path=str(log_cfg.get("file_path") or "").strip(),
    )
