#!/usr/bin/env bash
# 注册功能"合并友好"隔离自检。
# 断言：
#   1) 全部新增文件存在；
#   2) main 主导的高 churn 文件（api.ts / settings store / backup_service / services.config）
#      未被本功能污染（不含 register 引用）；
#   3) 两个一次性接入点已就位（feature_registry 接入 app.py、optionalNavItems 接入 top-nav）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== 检查新增文件 =="
NEW_FILES=(
  api/feature_registry.py
  api/register.py
  services/register/__init__.py
  services/register/config.py
  services/register/mail_provider.py
  services/register/openai_register.py
  services/register/service.py
  web/src/app/register/page.tsx
  web/src/app/register/components/register-card.tsx
  web/src/app/register/store.ts
  web/src/lib/register-api.ts
  web/src/config/nav.ts
)
for f in "${NEW_FILES[@]}"; do
  if [ ! -f "$f" ]; then
    echo "缺失新增文件: $f"
    exit 1
  fi
done
echo "新增文件齐全 (${#NEW_FILES[@]})"

echo "== 检查高 churn 文件未被污染 =="
# 注意：config.json 中原本就存在 backup.include.register（来自 main），不属于本功能改动，故不纳入检查。
for f in web/src/lib/api.ts web/src/app/settings/store.ts services/backup_service.py services/config.py; do
  if grep -qi "register" "$f" 2>/dev/null; then
    echo "警告: 高 churn 文件包含 register 引用: $f"
    exit 1
  fi
done
echo "高 churn 文件保持干净（未编辑 api.ts / settings store / backup_service / services.config）"

echo "== 检查一次性接入点存在 =="
grep -q "get_optional_routers" api/app.py || { echo "api/app.py 缺少 feature_registry 接入"; exit 1; }
grep -q "optionalNavItems" web/src/components/top-nav.tsx || { echo "top-nav.tsx 缺少 optionalNavItems 接入"; exit 1; }
echo "一次性接入点到位"

echo "OK: 注册功能保持合并友好（隔离）"
