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

# ── 清理可能由 conda / 系统注入的动态库路径 ──────────────────────────
# 若终端激活了 conda（如 (chatgpt2api)），conda 会把自带 OpenSSL 的
# DYLD_LIBRARY_PATH 注入所有子进程，覆盖掉 curl_cffi 捆绑的 BoringSSL，
# 触发 "TLS connect error: OPENSSL_internal:invalid library (0)"。
# 这里在启动 .venv 解释器前把这些变量清掉，保证 curl_cffi 用自带的 TLS 库。
unset DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH DYLD_INSERT_LIBRARIES DYLD_FRAMEWORK_PATH

cd "$(dirname "$0")/../.."   # 切到仓库根目录（脚本从 scripts/register-my/ 出发）
exec .venv/bin/python scripts/register-my/cli.py "$@"
