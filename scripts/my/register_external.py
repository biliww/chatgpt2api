"""外置注册脚本：复用项目注册链路，并把注册结果输出到可配置目标。

用法：
    uv run python scripts/my/register_external.py

脚本不接收命令行参数，默认读取同目录的 register_external.example.toml。
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
import tomllib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "register_external.example.toml"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

with contextlib.redirect_stdout(sys.stderr):
    from services.register import openai_register  # noqa: E402


def _now() -> str:
    """返回 UTC ISO 时间，便于跨机器汇总日志。"""
    return datetime.now(timezone.utc).isoformat()


def _mask_secret(value: Any, head: int = 8, tail: int = 4) -> str:
    """脱敏显示 token/key，避免日志泄露完整敏感信息。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= head + tail + 3:
        return "***"
    return f"{text[:head]}...{text[-tail:]}"


def _resolve_path(value: str | Path) -> Path:
    """把配置中的路径解析为绝对路径；相对路径按项目根目录计算。"""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_toml_file(path: Path) -> dict[str, Any]:
    """读取 TOML 配置文件，并保证顶层是对象。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"配置文件不存在: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"配置文件不是合法 TOML: {path}, {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("配置文件顶层必须是 TOML table")
    return data


def _dump_json_line(item: dict[str, Any]) -> str:
    """把一条记录编码成 JSONL 文本。"""
    return json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"


def _script_log(text: str, color: str = "") -> None:
    """把注册模块日志重定向到 stderr，避免污染 stdout JSONL 输出。"""
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    prefix = colors.get(color, "")
    suffix = "\033[0m" if prefix else ""
    print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}", file=sys.stderr, flush=True)


def _script_step(index: int, text: str, color: str = "") -> None:
    """用脚本日志格式输出单个注册任务的步骤。"""
    _script_log(f"[任务{index}] {text}", color)


@dataclass
class RemoteConfig:
    """远程 chatgpt2api 写入配置，描述目标地址、管理员密钥和请求超时。"""

    base_url: str = ""
    admin_key: str = ""
    timeout: float = 60.0

    def validate(self) -> None:
        """校验远程写入所需的基础参数。"""
        if not self.base_url:
            raise SystemExit("启用 remote sink 时必须配置 remote.base_url")
        if not self.base_url.startswith(("http://", "https://")):
            raise SystemExit("remote.base_url 必须以 http:// 或 https:// 开头")
        if not self.admin_key:
            raise SystemExit("启用 remote sink 时必须配置 remote.admin_key")


@dataclass
class OutputConfig:
    """注册结果输出配置，描述启用哪些 sink 以及本地文件路径。"""

    sinks: list[str] = field(default_factory=lambda: ["jsonl"])
    jsonl_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data/external-register-results.jsonl")
    json_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data/external-register-results.json")
    failed_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data/external-register-failed-imports.jsonl")
    remote: RemoteConfig = field(default_factory=RemoteConfig)


@dataclass
class ExternalRegisterConfig:
    """外置注册脚本总配置，聚合注册参数、邮箱配置和结果输出配置。"""

    total: int = 1
    threads: int = 1
    proxy: str = ""
    retry_per_task: int = 0
    dry_run: bool = False
    replay_failed_path: Path | None = None
    mail: dict[str, Any] = field(default_factory=dict)
    output: OutputConfig = field(default_factory=OutputConfig)

    @classmethod
    def from_file(cls, path: Path) -> "ExternalRegisterConfig":
        """从 TOML 文件创建配置。"""
        raw = _load_toml_file(path)
        register_raw = raw.get("register") if isinstance(raw.get("register"), dict) else raw
        output_raw = raw.get("output") if isinstance(raw.get("output"), dict) else {}
        remote_raw = output_raw.get("remote") if isinstance(output_raw.get("remote"), dict) else {}
        mail = raw.get("mail") if isinstance(raw.get("mail"), dict) else register_raw.get("mail", {})
        output = OutputConfig(
            sinks=[str(item).strip() for item in output_raw.get("sinks", ["jsonl"]) if str(item).strip()],
            jsonl_path=_resolve_path(str(output_raw.get("jsonl_path") or "data/external-register-results.jsonl")),
            json_path=_resolve_path(str(output_raw.get("json_path") or "data/external-register-results.json")),
            failed_path=_resolve_path(str(output_raw.get("failed_path") or "data/external-register-failed-imports.jsonl")),
            remote=RemoteConfig(
                base_url=str(remote_raw.get("base_url") or "").strip(),
                admin_key=str(remote_raw.get("admin_key") or "").strip(),
                timeout=float(remote_raw.get("timeout") or 60),
            ),
        )
        return cls(
            total=max(1, int(register_raw.get("total") or 1)),
            threads=max(1, int(register_raw.get("threads") or 1)),
            proxy=str(register_raw.get("proxy") or "").strip(),
            retry_per_task=max(0, int(register_raw.get("retry_per_task") or 0)),
            dry_run=bool(register_raw.get("dry_run")),
            replay_failed_path=_resolve_path(str(register_raw.get("replay_failed_path"))) if register_raw.get("replay_failed_path") else None,
            mail=dict(mail or {}),
            output=output,
        )

    def validate(self) -> None:
        """校验配置是否足够执行注册或重放。"""
        providers = self.mail.get("providers")
        if not isinstance(providers, list) or not any(isinstance(item, dict) and item.get("enable") for item in providers):
            raise SystemExit("mail.providers 至少需要一个启用的 provider")
        allowed_sinks = {"stdout", "jsonl", "json", "remote"}
        invalid = [item for item in self.output.sinks if item not in allowed_sinks]
        if invalid:
            raise SystemExit(f"不支持的 sink: {', '.join(invalid)}")
        if "remote" in self.output.sinks:
            self.output.remote.validate()

    def configure_openai_register(self) -> None:
        """把外置配置注入现有注册模块，但不触发本地号池写入。"""
        mail = dict(self.mail)
        if self.proxy:
            mail["proxy"] = self.proxy
        else:
            mail.pop("proxy", None)
        openai_register.config.update(
            {
                "mail": mail,
                "proxy": self.proxy,
                "total": self.total,
                "threads": self.threads,
            }
        )
        openai_register.log = _script_log
        openai_register.step = _script_step


class RegisterResultSink:
    """注册结果输出接口，所有具体 sink 都实现这个协议。"""

    def write_success(self, account: dict[str, Any]) -> None:
        """写入一个成功注册的账号。"""
        raise NotImplementedError

    def close(self) -> None:
        """释放 sink 资源，默认没有需要关闭的资源。"""
        return None


class StdoutJsonlSink(RegisterResultSink):
    """把成功账号以 JSONL 写到标准输出，适合管道消费。"""

    def __init__(self) -> None:
        """初始化线程锁，避免多线程输出交叉。"""
        self._lock = threading.Lock()

    def write_success(self, account: dict[str, Any]) -> None:
        """把完整账号写成一行 JSON 到 stdout。"""
        with self._lock:
            sys.stdout.write(_dump_json_line(account))
            sys.stdout.flush()


class FileJsonlSink(RegisterResultSink):
    """把成功账号追加到本地 JSONL 文件，适合持续备份。"""

    def __init__(self, path: Path) -> None:
        """初始化输出路径和线程锁。"""
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_success(self, account: dict[str, Any]) -> None:
        """把完整账号追加到 JSONL 文件。"""
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(_dump_json_line(account))


class FileJsonSink(RegisterResultSink):
    """把成功账号收集为 JSON 数组，并在结束时写入文件。"""

    def __init__(self, path: Path) -> None:
        """初始化输出路径、内存列表和线程锁。"""
        self.path = path
        self._items: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_success(self, account: dict[str, Any]) -> None:
        """把账号暂存到内存列表，等待 close 时统一写入。"""
        with self._lock:
            self._items.append(dict(account))

    def close(self) -> None:
        """把收集到的账号数组写入 JSON 文件。"""
        with self._lock:
            self.path.write_text(json.dumps(self._items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class FailureJsonlSink:
    """记录注册失败或远程导入失败，便于后续重放处理。"""

    def __init__(self, path: Path) -> None:
        """初始化失败记录文件路径和线程锁。"""
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_failure(self, reason: str, account: dict[str, Any] | None = None, index: int | None = None) -> None:
        """追加一条失败记录，账号存在时保留完整账号用于重放。"""
        record = {"time": _now(), "reason": str(reason)}
        if index is not None:
            record["index"] = index
        if account is not None:
            record["account"] = account
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(_dump_json_line(record))


class RemoteChatGPT2APISink(RegisterResultSink):
    """把成功账号推送到远程 chatgpt2api 的 /api/accounts 接口。"""

    def __init__(self, config: RemoteConfig) -> None:
        """初始化远程地址、管理员密钥和请求超时。"""
        self.config = config
        self.base_url = config.base_url.rstrip("/")

    def write_success(self, account: dict[str, Any]) -> None:
        """提交完整账号到远程实例，并校验远程导入结果。"""
        url = f"{self.base_url}/api/accounts"
        body = json.dumps({"accounts": [account]}, ensure_ascii=False).encode("utf-8")
        request = urllib_request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.admin_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib_request.urlopen(request, timeout=self.config.timeout) as response:
                status = int(response.status)
                text = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"remote_import_http_{exc.code}: {detail}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"remote_import_error: {exc.reason}") from exc
        if status < 200 or status >= 300:
            raise RuntimeError(f"remote_import_http_{status}: {text[:500]}")
        data = json.loads(text) if text.strip() else {}
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            raise RuntimeError(f"remote_import_errors: {errors}")


class CompositeSink(RegisterResultSink):
    """组合多个输出目标，让一次注册结果可以同时写文件和推远程。"""

    def __init__(self, sinks: list[RegisterResultSink], failure_sink: FailureJsonlSink) -> None:
        """初始化子 sink 列表和失败记录 sink。"""
        self.sinks = sinks
        self.failure_sink = failure_sink

    def write_success(self, account: dict[str, Any]) -> None:
        """把账号写入所有子 sink，某个 sink 失败时只记录失败，不触发重新注册。"""
        for sink in self.sinks:
            try:
                sink.write_success(account)
            except Exception as exc:
                self.failure_sink.write_failure(str(exc), account=account)
                _script_log(f"结果写入失败: {exc}", "red")

    def close(self) -> None:
        """关闭所有子 sink。"""
        for sink in self.sinks:
            sink.close()


class RegisterJobRunner:
    """注册任务运行器，负责并发执行注册、重试和统计汇总。"""

    def __init__(self, config: ExternalRegisterConfig, sink: CompositeSink, failure_sink: FailureJsonlSink) -> None:
        """保存配置和输出目标，并初始化统计计数。"""
        self.config = config
        self.sink = sink
        self.failure_sink = failure_sink
        self._lock = threading.Lock()
        self.success = 0
        self.fail = 0

    def run(self) -> int:
        """启动线程池执行所有注册任务，并返回进程退出码。"""
        started = time.time()
        submitted = 0
        futures = set()
        with ThreadPoolExecutor(max_workers=self.config.threads) as executor:
            while submitted < self.config.total or futures:
                while submitted < self.config.total and len(futures) < self.config.threads:
                    submitted += 1
                    futures.add(executor.submit(self._run_one_with_retry, submitted))
                finished, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in finished:
                    ok = bool(future.result().get("ok"))
                    with self._lock:
                        if ok:
                            self.success += 1
                        else:
                            self.fail += 1
                    _script_log(f"进度: 成功={self.success}, 失败={self.fail}, 已提交={submitted}/{self.config.total}", "yellow")
        self.sink.close()
        elapsed = time.time() - started
        _script_log(f"任务结束: 成功={self.success}, 失败={self.fail}, 耗时={elapsed:.1f}s", "green" if self.fail == 0 else "yellow")
        return 0 if self.fail == 0 else 1

    def _run_one_with_retry(self, index: int) -> dict[str, Any]:
        """执行单个注册任务，并按配置进行有限重试。"""
        attempts = self.config.retry_per_task + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            result = self._run_one(index, attempt)
            if result.get("ok"):
                return result
            last_error = str(result.get("error") or "")
            if attempt < attempts:
                _script_log(f"[任务{index}] 准备重试 {attempt}/{self.config.retry_per_task}: {last_error}", "yellow")
                time.sleep(1)
        self.failure_sink.write_failure(last_error or "register_failed", index=index)
        return {"ok": False, "index": index, "error": last_error}

    def _run_one(self, index: int, attempt: int) -> dict[str, Any]:
        """直接调用 PlatformRegistrar.register，成功后交给自定义 sink，不写本地号池。"""
        registrar = openai_register.PlatformRegistrar(self.config.proxy)
        started = time.time()
        try:
            _script_step(index, f"任务启动 attempt={attempt}")
            account = registrar.register(index)
            self.sink.write_success(account)
            cost = time.time() - started
            email = account.get("email") or "(未知邮箱)"
            token_preview = _mask_secret(account.get("access_token"))
            _script_log(f"[任务{index}] 注册成功: email={email}, access_token={token_preview}, 耗时={cost:.1f}s", "green")
            return {"ok": True, "index": index, "result": account}
        except Exception as exc:
            cost = time.time() - started
            _script_log(f"[任务{index}] 注册失败 attempt={attempt}, 耗时={cost:.1f}s, 原因: {exc}", "red")
            return {"ok": False, "index": index, "error": str(exc)}
        finally:
            registrar.close()


class FailedImportReplayer:
    """失败导入重放器，读取失败 JSONL 并重新推送到远程实例。"""

    def __init__(self, path: Path, remote_sink: RemoteChatGPT2APISink, failure_sink: FailureJsonlSink) -> None:
        """保存失败文件路径、远程 sink 和新的失败记录 sink。"""
        self.path = path
        self.remote_sink = remote_sink
        self.failure_sink = failure_sink

    def run(self) -> int:
        """执行失败记录重放，并返回进程退出码。"""
        if not self.path.exists():
            raise SystemExit(f"失败记录文件不存在: {self.path}")
        ok = 0
        fail = 0
        for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            account: dict[str, Any] | None = None
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                account = record.get("account") if isinstance(record, dict) else None
                if not isinstance(account, dict) or not account.get("access_token"):
                    continue
                self.remote_sink.write_success(account)
                ok += 1
                _script_log(f"重放成功 line={line_no}, token={_mask_secret(account.get('access_token'))}", "green")
            except Exception as exc:
                fail += 1
                self.failure_sink.write_failure(f"replay_failed_line_{line_no}: {exc}", account=account if isinstance(account, dict) else None)
                _script_log(f"重放失败 line={line_no}, 原因: {exc}", "red")
        _script_log(f"重放结束: 成功={ok}, 失败={fail}", "green" if fail == 0 else "yellow")
        return 0 if fail == 0 else 1


def _build_sink(config: ExternalRegisterConfig) -> tuple[CompositeSink, FailureJsonlSink]:
    """根据配置创建组合输出 sink 和失败记录 sink。"""
    failure_sink = FailureJsonlSink(config.output.failed_path)
    sinks: list[RegisterResultSink] = []
    for name in config.output.sinks:
        if name == "stdout":
            sinks.append(StdoutJsonlSink())
        elif name == "jsonl":
            sinks.append(FileJsonlSink(config.output.jsonl_path))
        elif name == "json":
            sinks.append(FileJsonSink(config.output.json_path))
        elif name == "remote":
            sinks.append(RemoteChatGPT2APISink(config.output.remote))
    if not sinks:
        raise SystemExit("至少需要配置一个输出 sink")
    return CompositeSink(sinks, failure_sink), failure_sink


def main() -> int:
    """脚本入口：读取同目录 TOML 配置并执行注册。"""
    if len(sys.argv) > 1:
        raise SystemExit("此脚本不支持命令行参数，请修改同目录 register_external.example.toml 后直接运行。")
    config = ExternalRegisterConfig.from_file(DEFAULT_CONFIG_FILE)
    if config.replay_failed_path:
        config.output.remote.validate()
        failure_sink = FailureJsonlSink(config.output.failed_path)
        _script_log(f"读取配置: {DEFAULT_CONFIG_FILE}", "yellow")
        return FailedImportReplayer(config.replay_failed_path, RemoteChatGPT2APISink(config.output.remote), failure_sink).run()
    config.validate()
    config.configure_openai_register()
    _script_log(f"读取配置: {DEFAULT_CONFIG_FILE}", "yellow")
    if config.dry_run:
        _script_log(
            f"配置校验通过: total={config.total}, threads={config.threads}, proxy={'已配置' if config.proxy else '未配置'}, sinks={config.output.sinks}",
            "green",
        )
        return 0
    sink, failure_sink = _build_sink(config)
    return RegisterJobRunner(config, sink, failure_sink).run()


if __name__ == "__main__":
    raise SystemExit(main())
