"""将注册机产出的账号导入到本项目号池（data/accounts.json / 对应 storage 后端）。

背景
----
浏览器注册机（scripts/register-my）注册成功后，结果写在
    scripts/register-my/output/accounts.jsonl
每行一条 JSON 账号记录，字段示例：
    {
      "email": "...", "password": "...",
      "access_token": "eyJ...", "refresh_token": "...", "id_token": "...",
      "session_token": "...", "source_type": "browser",
      "created_at": "2026-07-17T20:15:42+00:00"
    }

本脚本读取上述来源，调用项目统一的 services.account_service.add_account_items()
写入号池。account_service 会自动：
  - 以 access_token 去重（已存在的账号会被合并/跳过，不重复写入）；
  - 归一化字段（type/free、status/正常、quota 等）；
  - 按当前项目 storage 后端落盘（默认 JSON，也可 git/sqlite，取决于 STORAGE_BACKEND 环境变量）。

可用 --verify 在导入后调用 refresh_accounts() 做一次后端校验，填充 status/quota
（新账号通常 quota=25）。无网或不想联网时加 --no-verify。

用法
----
    # 默认导入 output/accounts.jsonl 并校验
    python scripts/register-my/import_accounts.py

    # 指定来源文件（JSONL / JSON 数组 / 单条 JSON 都支持）
    python scripts/register-my/import_accounts.py --source /path/to/accounts.jsonl

    # 只预览、不写入
    python scripts/register-my/import_accounts.py --dry-run

    # 不校验（离线导入）
    python scripts/register-my/import_accounts.py --no-verify

    # 强制覆盖 source_type 字段为 web
    python scripts/register-my/import_accounts.py --source-type web
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_SOURCE = PROJECT_ROOT / "scripts" / "register-my" / "output" / "accounts.jsonl"


def _load_records(source: Path) -> list[dict]:
    """从来源文件解析账号记录，支持 JSONL / JSON 数组 / 单条 JSON。

    返回记录列表（未做字段校验，缺 access_token 的会在导入阶段被跳过并统计）。
    """
    if not source.exists():
        raise FileNotFoundError(f"来源文件不存在: {source}")
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        return []

    # 优先按「整文件是一个 JSON」解析
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 退化为逐行 JSONL
        records: list[dict] = []
        for ln, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {ln} 行 JSON 解析失败: {exc}") from exc
            if isinstance(obj, dict):
                records.append(obj)
            elif isinstance(obj, list):
                records.extend(o for o in obj if isinstance(o, dict))
            else:
                raise ValueError(f"第 {ln} 行不是对象/数组: {type(obj).__name__}")
        return records

    if isinstance(data, list):
        return [o for o in data if isinstance(o, dict)]
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"来源文件顶层类型不支持: {type(data).__name__}")


def import_accounts(
    source: Path,
    *,
    verify: bool = True,
    dry_run: bool = False,
    source_type: str | None = None,
    log: object | None = None,
) -> dict:
    """导入来源文件中的账号到项目号池。

    参数
    ----
    source:        来源文件路径（JSONL / JSON 数组 / 单条 JSON）
    verify:        导入后是否调用 refresh_accounts 后端校验（填充 status/quota）
    dry_run:       仅预览不写入
    source_type:   指定该值则覆盖每条记录的 source_type
    log:           可选的日志对象，需有 info/warn/error 方法

    返回统计字典：{total, valid, invalid, added, skipped, ...}
    """
    def _say(level: str, msg: str) -> None:
        if log is not None and hasattr(log, level):
            getattr(log, level)(msg)
        else:
            prefix = {"warn": "⚠ ", "error": "✗ ", "info": ""}.get(level, "")
            print(f"{prefix}{msg}", file=sys.stderr if level in ("warn", "error") else sys.stdout)

    raw = _load_records(source)
    _say("info", f"已读取来源 {source.name}: 共 {len(raw)} 条记录")

    valid: list[dict] = []
    invalid = 0
    for rec in raw:
        if not isinstance(rec, dict):
            invalid += 1
            continue
        if not str(rec.get("access_token") or rec.get("accessToken") or "").strip():
            invalid += 1
            _say("warn", f"跳过无效记录（缺 access_token）: {rec.get('email') or rec.get('access_token', '')[:16]}")
            continue
        item = dict(rec)
        if source_type:
            item["source_type"] = source_type
        valid.append(item)

    if invalid:
        _say("warn", f"{invalid} 条记录因缺 access_token 被跳过")

    if dry_run:
        _say("info", f"[dry-run] 将导入 {len(valid)} 条有效账号，不实际写入号池。")
        return {"total": len(raw), "valid": len(valid), "invalid": invalid,
                "added": 0, "skipped": 0, "dry_run": True}

    with contextlib.redirect_stdout(sys.stderr):
        from services.account_service import account_service

        result = account_service.add_account_items(valid)
        added = int(result.get("added") or 0)
        skipped = int(result.get("skipped") or 0)
        _say("info", f"写入号池完成：新增 {added}，跳过（已存在）{skipped}，现有 {len(result.get('items', []))} 个账号")

        if verify and valid:
            tokens = [str(v.get("access_token") or "").strip() for v in valid]
            _say("info", "开始后端校验（refresh_accounts）…")
            acc = account_service.refresh_accounts(tokens)
            refreshed = int(acc.get("refreshed") or 0)
            errors = acc.get("errors") or []
            _say("info", f"校验完成：成功 {refreshed}，错误 {len(errors)}")
            for err in errors[:10]:
                _say("warn", f"  校验失败: {err}")
            stats = account_service.get_stats()
            _say("info", f"号池统计: 总数={stats['total']} 正常={stats['active']} 限流={stats['limited']} "
                          f"异常={stats['abnormal']} 禁用={stats['disabled']} 总配额={stats['total_quota']}")
            return {"total": len(raw), "valid": len(valid), "invalid": invalid,
                    "added": added, "skipped": skipped,
                    "refreshed": refreshed, "errors": len(errors),
                    "stats": stats}

    return {"total": len(raw), "valid": len(valid), "invalid": invalid,
            "added": added, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description="将注册机产出的账号导入本项目号池")
    parser.add_argument("--source", "-s", default=str(DEFAULT_SOURCE),
                        help=f"来源文件（JSONL/JSON 数组/单条 JSON），默认 {DEFAULT_SOURCE}")
    parser.add_argument("--verify", dest="verify", action="store_true", default=True,
                        help="导入后做后端校验（默认开启）")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="导入后不做后端校验（离线场景）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入号池")
    parser.add_argument("--source-type", default=None,
                        help="覆盖每条记录的 source_type（如 web / browser / codex）")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    try:
        stats = import_accounts(
            source,
            verify=args.verify,
            dry_run=args.dry_run,
            source_type=args.source_type.strip() if args.source_type else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
