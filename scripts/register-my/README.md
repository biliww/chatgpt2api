# 浏览器注册机（register-my）

使用**真实无头/有头浏览器（Playwright + Chromium）**走完 OpenAI 注册 UI，并复用项目既有
邮箱架构（`services/register/mail_provider`，多 provider、默认 `tempmail_lol`）完成验证码验证，
最后从浏览器会话中提取登录态产出账号。

## 与现有 Web 注册机的关系

| 维度 | Web 注册机（`services/register`） | 浏览器注册机（`scripts/register-my`） |
|------|-----------------------------------|----------------------------------------|
| 注册方式 | curl_cffi 模拟 Auth0 / OpenAI API | 真实浏览器走 UI |
| 反爬 | 手写 Sentinel PoW / SO-Token | 浏览器原生处理 Cloudflare / Turnstile |
| 邮箱验证 | 复用 `mail_provider` | **同样复用 `mail_provider`** |
| 运行形态 | Web 面板 + 后端线程池 | 独立脚本，可命令行 / 定时任务运行 |
| 结果输出 | 写本地号池 | 号池 / JSONL / 远程（可配） |

两者**共用同一套邮箱服务商逻辑**，新增 / 修复邮箱 provider 只需改一处。

## 安装

```bash
pip install -r scripts/register-my/requirements-register-my.txt
playwright install chromium     # 必须：下载 Chromium 二进制（约 150MB）
```

## 运行

```bash
# 无头模式，复用项目根 config-register.json 的邮箱/代理配置
python scripts/register-my/cli.py

# 有头模式（macOS 直接可跑，便于首次校准选择器），注册 3 个
python scripts/register-my/cli.py --headed --total 3

# 指定独立配置
python scripts/register-my/cli.py --config scripts/register-my/register-my.example.json

# 仅校验配置
python scripts/register-my/cli.py --dry-run
```

## 目录结构

| 文件 | 职责 |
|------|------|
| `browser.py` | Playwright 浏览器封装（无头/有头、隐匿、单上下文隔离） |
| `mailbox.py` | 薄封装，直接复用 `services.register.mail_provider` |
| `config.py` | 配置加载（复用 config-register.json 结构 + 扩展 browser/output 段） |
| `register.py` | 注册编排：浏览器 UI 流程 + 邮箱 OTP + token 提取 |
| `cli.py` | 命令行入口、并发线程池、结果输出（号池/JSONL/远程） |
| `register-my.example.json` | 配置模板 |

## 重要提醒

OpenAI 注册页 DOM / 文案随版本变化较快，`register.py` 中的选择器采用“语义定位 + 多候选 +
失败截图”策略。**首次上线前请用 `--headed` 模式跑通一次**，必要时按实际页面调整选择器
（集中在 `_signup` / `_fill_about_you` 两个方法）。
