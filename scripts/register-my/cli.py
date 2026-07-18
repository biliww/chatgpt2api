"""浏览器注册机命令行入口。

用法示例：
    # 无头模式，复用项目根 config-register.json 的邮箱/代理配置
    python scripts/register-my/cli.py

    # 有头模式（需本地有显示器；macOS 可直接运行），注册 3 个
    python scripts/register-my/cli.py --headed --total 3

    # 指定独立配置文件
    python scripts/register-my/cli.py --config scripts/register-my/register-my.example.json

    # 只校验配置不真正注册
    python scripts/register-my/cli.py --dry-run

说明：
    - 邮箱验证完整复用 services.register.mail_provider（多 provider，默认 tempmail_lol）。
    - 浏览器驱动基于 Playwright，需先安装：pip install playwright && playwright install chromium。
    - 注册成功结果可写入本地号池 / JSONL / 远程 /api/accounts（见配置 output 段）。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

with contextlib.redirect_stdout(sys.stderr):
    from config import load_config  # noqa: E402
    from logger import make_logger  # noqa: E402
    from register import run_one  # noqa: E402


def _mask(value: str, head: int = 8, tail: int = 4) -> str:
    value = str(value or "")
    if len(value) <= head + tail + 2:
        return "***"
    return f"{value[:head]}...{value[-tail:]}"


def _resolve_admin_key(out_cfg: dict, project_root: Path) -> str:
    """解析用于调用 /api/accounts 的管理员密钥，按优先级：
    1) output.remote_admin_key 显式配置
    2) 环境变量 CHATGPT2API_AUTH_KEY
    3) 回退读取项目根 config.json 的 auth-key
    """
    key = str(out_cfg.get("remote_admin_key") or "").strip()
    if key:
        return key
    env_key = os.environ.get("CHATGPT2API_AUTH_KEY", "").strip()
    if env_key:
        return env_key
    try:
        main_cfg = json.loads((project_root / "config.json").read_text(encoding="utf-8"))
        ak = str(main_cfg.get("auth-key") or "").strip()
        if ak:
            return ak
    except Exception:
        pass
    return ""


def _push_remote(base_url: str, admin_key: str, account: dict) -> dict:
    """把账号推送到正在运行的 Web 服务的 /api/accounts（与前端“导入”按钮同一个接口）。

    由主程序在自己的进程内完成 add_account_items + refresh_accounts，
    缓存天然同步，前端 /accounts/ 立即可见，注册脚本不依赖主程序内部实现。
    """
    import urllib.request

    url = f"{base_url.rstrip('/')}/api/accounts"
    payload = json.dumps({"accounts": [account]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {admin_key}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器注册机（无头/有头）")
    parser.add_argument("--config", default="", help="独立配置文件路径（JSON），默认用项目根 config-register.json")
    parser.add_argument("--total", type=int, default=0, help="本次注册数量（覆盖配置）")
    parser.add_argument("--threads", type=int, default=0, help="并发线程数（覆盖配置）")
    parser.add_argument("--proxy", default="", help="代理（覆盖配置 browser.proxy）")
    parser.add_argument("--headless", action="store_true", help="无头模式（默认）")
    parser.add_argument("--headed", action="store_true", help="有头模式（需显示器）")
    parser.add_argument("--dry-run", action="store_true", help="只校验配置，不真正注册")
    args = parser.parse_args()

    cfg = load_config(args.config or None)
    if args.total:
        cfg["total"] = max(1, args.total)
    if args.threads:
        cfg["threads"] = max(1, args.threads)
    if args.proxy:
        cfg["browser"]["proxy"] = args.proxy
    if args.headed:
        cfg["browser"]["headless"] = False
    if args.headless:
        cfg["browser"]["headless"] = True

    log = make_logger(cfg)
    log.info(f"日志级别: {cfg.get('log', {}).get('level', 'INFO')}")
    providers = []
    for p in cfg.get("mail", {}).get("providers", []) or []:
        if p.get("enable"):
            providers.append(str(p.get("type")))
    log(f"配置：邮箱服务商={providers}（默认优先 tempmail_lol）")
    log(f"配置：浏览器={'有头' if not cfg['browser']['headless'] else '无头'}，"
        f"代理={cfg['browser']['proxy'] or '无'}，total={cfg['total']}，threads={cfg['threads']}")

    if args.dry_run:
        log("dry-run：配置校验通过，未执行注册。", "green")
        return 0

    out = cfg.get("output", {})
    jsonl_path = str(out.get("jsonl_path") or "").strip()
    if jsonl_path:
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
    remote_base = str(out.get("remote_base_url") or "").strip()
    remote_key = _resolve_admin_key(out, PROJECT_ROOT)
    if remote_base:
        if remote_key:
            log(f"配置：远程推送已启用 → {remote_base}/api/accounts（鉴权：来自 auth-key）")
        else:
            log(f"配置：远程推送目标={remote_base}，但未找到管理员密钥，将只写 jsonl 备份。", "yellow")
    else:
        log("配置：未设置 output.remote_base_url，注册成功仅写入本地 jsonl 备份，不推送 Web 服务。", "yellow")

    def sink(account: dict) -> None:
        # 1) 始终写一份 jsonl 备份（即使 Web 服务未启动，也可稍后在前端导入）
        if jsonl_path:
            with open(jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(account, ensure_ascii=False) + "\n")
        # 2) 通过 HTTP 接口推送给正在运行的 Web 服务（与前端“导入”按钮同一接口）
        if remote_base:
            if not remote_key:
                log("⚠️ 未配置管理员密钥（output.remote_admin_key / CHATGPT2API_AUTH_KEY / config.json 的 auth-key），"
                    "无法推送 /api/accounts；账号仅写入 jsonl 备份。", "yellow")
            else:
                resp = _push_remote(remote_base, remote_key, account)
                if resp.get("ok") is False:
                    log(f"⚠️ 推送到 Web 服务失败（{remote_base}）：{resp.get('error')}；"
                        f"账号已写入 jsonl 备份，可在前端 /accounts/ 导入或启动 Web 服务后重推。", "yellow")
                else:
                    added = int(resp.get("added") or 0)
                    skipped = int(resp.get("skipped") or 0)
                    if added:
                        log(f"已推送到 Web 服务 /api/accounts（前端立即可见）: {account.get('email')}", "green")
                    elif skipped:
                        log(f"号池已存在，跳过: {account.get('email')}", "yellow")

    success = fail = 0
    total = int(cfg["total"])
    threads = int(cfg["threads"])
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = set()
        submitted = 0
        lock = threading.Lock()

        def submit_next() -> None:
            nonlocal submitted
            if submitted < total:
                submitted += 1
                futures.add(pool.submit(run_one, cfg, submitted, log))

        while submitted < total or futures:
            while len(futures) < threads and submitted < total:
                submit_next()
            if not futures:
                break
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for fut in done:
                res = fut.result()
                if res.get("ok"):
                    success += 1
                    acc = res.get("result", {})
                    log(f"账号产出: {acc.get('email')} / access_token={_mask(acc.get('access_token',''))}", "green")
                    sink(acc)
                else:
                    fail += 1
                    log(f"任务{res.get('index')} 失败: {res.get('error')}", "red")

    log(f"注册结束：成功={success}，失败={fail}", "green")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
