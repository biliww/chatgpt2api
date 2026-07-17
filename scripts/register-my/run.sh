#!/usr/bin/env bash
# 浏览器注册机 —— 一键运行脚本
# 用法：
#   ./run.sh                 # 无头批量（用 config.json 里的 total/threads）
#   ./run.sh --dry-run       # 仅校验配置
#   ./run.sh --headed --total 1   # 首次建议：有头跑 1 个，观察并校准选择器
#   ./run.sh --headless --total 10 --threads 3   # 无头批量
#
# 说明：脚本自动使用项目 .venv 里的 python（已含 curl_cffi + playwright + chromium）。
set -euo pipefail
cd "$(dirname "$0")/../.."   # 切到仓库根目录（脚本从 scripts/register-my/ 出发）
exec .venv/bin/python scripts/register-my/cli.py "$@"
