# 注册功能外置脚本提取与远程写入方案

## 背景

当前注册机运行在 Web 面板 `/register/` 中，注册成功后会直接写入本项目本地号池：

```python
account_service.add_account_items([result])
account_service.refresh_accounts([access_token])
```

这个模式适合本地部署的一体化使用，但不适合下面的场景：

- 在一台机器上单独跑注册脚本；
- 注册结果需要导入远程服务器上的 chatgpt2api；
- 不希望注册脚本直接修改当前项目的 `data/accounts.json`；
- 希望注册结果先落盘、人工检查后再导入；
- 希望注册脚本输出标准 JSON，交给其他系统消费。

因此需要在 `/Users/wangpenglong/projects/github/chatgpt2api/scripts/my` 下新增一个独立脚本，把现有注册链路复用出来，但将“成功后写入本地号池”改成可配置输出。

## 目标

新增脚本建议路径：

```text
scripts/my/register_external.py
```

脚本目标：

1. 复用当前项目已有注册逻辑，避免复制大段注册协议代码；
2. 支持通过独立配置文件运行，不强依赖 Web 面板；
3. 注册成功后不直接写入当前项目本地号池；
4. 支持多种输出目标：
   - 打印 JSONL 到 stdout；
   - 写入本地 JSONL 文件；
   - 写入本地 JSON 文件；
   - 推送到远程 chatgpt2api 的 `/api/accounts`；
5. 支持注册代理、邮箱 provider、注册总数、线程数、重试策略；
6. 支持 dry-run 和导入失败时的本地备份。

## 非目标

本次脚本不建议重新实现注册协议本身。

不建议把下面逻辑复制成第二套：

- Auth0 / OpenAI authorize；
- PKCE；
- Sentinel token；
- email OTP；
- token exchange；
- 邮箱 provider 适配。

原因是这些逻辑变化快，复制后会变成两套维护点。脚本应该调用现有模块，只替换结果写入方式。

## 当前注册链路梳理

### 核心文件

```text
services/register/openai_register.py
services/register/mail_provider.py
services/register_service.py
api/register.py
web/src/app/register/components/register-card.tsx
```

### 注册任务流程

`PlatformRegistrar.register(index)` 当前完成完整注册链路：

```text
创建邮箱
  -> platform authorize
  -> 提交注册密码
  -> 发送邮箱验证码
  -> 等待邮箱验证码
  -> 校验验证码
  -> 创建账号资料
  -> 换取 access_token / refresh_token / id_token
  -> 返回账号 payload
```

返回结构类似：

```json
{
  "email": "example@example.com",
  "password": "random-password",
  "access_token": "...",
  "refresh_token": "...",
  "id_token": "...",
  "source_type": "web",
  "created_at": "2026-06-12T00:00:00+00:00"
}
```

### 当前本地写入点

当前 `worker(index)` 中注册成功后会直接写本地号池：

```python
access_token = str(result["access_token"])
account_service.add_account_items([result])
refresh_result = account_service.refresh_accounts([access_token])
```

外置脚本要绕开这一段，不调用当前 `worker(index)`，而是直接调用：

```python
registrar = PlatformRegistrar(config["proxy"])
result = registrar.register(index)
```

这样可以拿到注册结果，但不会自动写入本地号池。

## 推荐实现方式

### 方案选择

推荐实现为：

```text
scripts/my/register_external.py
  -> 导入 services.register.openai_register
  -> 更新 openai_register.config
  -> 直接调用 PlatformRegistrar.register(index)
  -> 根据 sink 配置处理 result
```

不推荐：

```text
scripts/my/register_external.py
  -> 调用 openai_register.worker(index)
```

因为 `worker(index)` 会写入本地 `account_service`。

### 代码结构建议

脚本内部拆成这些类和方法：

```text
ExternalRegisterConfig
  解析脚本同目录的 TOML 配置文件。

RegisterJobRunner
  按 total / threads 执行注册任务。

RegisterResultSink
  结果输出接口。

StdoutJsonlSink
  每个成功结果打印一行 JSON。

FileJsonlSink
  每个成功结果追加到 JSONL 文件。

FileJsonSink
  将结果数组写入 JSON 文件。

RemoteChatGPT2APISink
  调用远程 /api/accounts 导入账号。

CompositeSink
  同时写多个 sink，例如远程导入 + 本地备份。
```

用户要求“每个类的开头都要注释功能和作用、每个方法要有良好的中文注释”，所以实际脚本中的类和方法需要加中文注释。

## 配置文件设计

固定配置路径：

```text
scripts/my/register_external.example.toml
```

使用 TOML 是因为 TOML 支持注释，可以直接在配置文件中解释每个字段。脚本不接受命令行参数，所有行为都通过该文件控制。

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `register.total` | number | 本次注册目标数量 |
| `register.threads` | number | 并发线程数 |
| `register.proxy` | string | 注册链路和邮箱 provider 使用的代理 |
| `register.retry_per_task` | number | 单个任务失败后的本地重试次数 |
| `register.dry_run` | boolean | 只校验配置，不执行真实注册 |
| `register.replay_failed_path` | string | 填写后只重放失败导入记录 |
| `mail` | object | 复用当前注册机邮箱配置格式 |
| `output.sinks` | string[] | 输出目标，支持 `stdout`、`jsonl`、`json`、`remote` |
| `output.jsonl_path` | string | JSONL 输出路径 |
| `output.json_path` | string | JSON 数组输出路径 |
| `output.remote.base_url` | string | 远程 chatgpt2api 地址 |
| `output.remote.admin_key` | string | 远程管理员密钥 |
| `output.remote.timeout` | number | 远程导入请求超时 |
当前远程 `/api/accounts` 接口会自动刷新账号状态。

## 运行方式

脚本不支持命令行参数，直接运行：

```bash
uv run python scripts/my/register_external.py
```

需要修改注册数、代理、输出目标或远程地址时，直接编辑 `scripts/my/register_external.example.toml`。

## 远程写入方式

远程部署的本项目已经提供账号导入接口：

```text
POST /api/accounts
Authorization: Bearer <admin-key>
Content-Type: application/json
```

请求体可以提交完整账号：

```json
{
  "accounts": [
    {
      "email": "example@example.com",
      "password": "random-password",
      "access_token": "...",
      "refresh_token": "...",
      "id_token": "...",
      "source_type": "web",
      "created_at": "2026-06-12T00:00:00+00:00"
    }
  ]
}
```

也可以只提交 token：

```json
{
  "tokens": ["access-token"]
}
```

本方案建议提交完整 `accounts`，因为这样远程号池能保存 `refresh_token` 和 `id_token`，后续可以刷新 access token。

### 远程导入响应处理

脚本需要识别：

```json
{
  "added": 1,
  "skipped": 0,
  "refreshed": 1,
  "errors": [],
  "items": []
}
```

建议逻辑：

- HTTP 2xx 且 `errors` 为空：视为远程写入成功；
- HTTP 2xx 但 `errors` 非空：本地标记为部分成功，并写入失败备份；
- HTTP 非 2xx 或请求异常：写入本地失败备份，日志提示可手动重放。

## 本地备份与失败恢复

即使启用远程写入，也建议始终写一份本地 JSONL 备份：

```text
data/external-register-results.jsonl
data/external-register-failed-imports.jsonl
```

成功注册但远程导入失败时，写入：

```json
{
  "time": "2026-06-12T00:00:00+00:00",
  "reason": "remote_import_http_500",
  "account": {
    "email": "example@example.com",
    "access_token": "...",
    "refresh_token": "...",
    "id_token": "..."
  }
}
```

需要重放时，在 TOML 中填写：

```toml
[register]
replay_failed_path = "data/external-register-failed-imports.jsonl"

[output.remote]
base_url = "https://your-chatgpt2api.example.com"
admin_key = "sk-admin-xxx"
```

然后直接运行 `uv run python scripts/my/register_external.py`。

## 运行时流程

```text
读取配置
  -> 校验 mail.providers 至少一个启用
  -> 将 mail/proxy/total/threads 写入 openai_register.config
  -> 创建线程池
  -> 每个任务直接调用 PlatformRegistrar.register(index)
  -> 成功后交给 sink 写出
  -> 失败记录错误日志
  -> 输出汇总统计
```

### 单任务伪代码

```python
def run_one(index: int) -> dict:
    registrar = openai_register.PlatformRegistrar(proxy)
    try:
        result = registrar.register(index)
        sink.write_success(result)
        return {"ok": True, "result": result}
    except Exception as exc:
        sink.write_failure(index, exc)
        return {"ok": False, "error": str(exc)}
    finally:
        registrar.close()
```

## 与现有 Web 注册机的区别

| 维度 | Web 注册机 | 外置脚本 |
|------|------------|----------|
| 配置来源 | `data/register.json` + Web 表单 | 脚本同目录 TOML 配置 |
| 成功后行为 | 写当前项目本地号池 | 可选 stdout/file/remote |
| 实时日志 | SSE 推送到页面 | 控制台日志 |
| 状态统计 | 保存在 `register_service` | 脚本内存统计 |
| 适合场景 | 当前实例自用 | 注册结果导入远程实例 |

## 需要注意的实现细节

### 1. 不要导入 `register_service`

脚本不应该导入：

```python
from services.register_service import register_service
```

因为它会绑定 Web 注册机状态和本地持久化文件。

### 2. 不要调用 `openai_register.worker`

`worker` 会写本地号池，所以外置脚本不能调用它。

### 3. 可以复用 `mail_provider`

邮箱 provider 的配置格式与 Web 注册机保持一致，这样可以直接复用当前实现。

### 4. 代理需要同步给邮箱配置

现有 Web 注册机会把 `register.proxy` 注入 `mail.proxy`。脚本也需要做同样处理：

```python
if proxy:
    mail["proxy"] = proxy
else:
    mail.pop("proxy", None)
```

这里要特别处理 `proxy` 清空的情况，避免旧配置残留。

### 5. 远程地址规范化

`remote.base_url` 需要去掉末尾 `/`：

```text
https://example.com/
-> https://example.com
```

最终请求：

```text
https://example.com/api/accounts
```

### 6. 敏感信息脱敏

日志不要完整打印：

- `access_token`
- `refresh_token`
- `id_token`
- 邮箱 API Key
- 远程 admin key

建议只显示前后几位：

```text
eyJhbGci...abcd
```

## 推荐脚本落地顺序

第一阶段：

1. 新增 `scripts/my/register_external.py`；
2. 支持读取带注释的 TOML 配置；
3. 支持 `stdout` 和 `jsonl` sink；
4. 直接调用 `PlatformRegistrar.register(index)`；
5. 不写本地号池。

第二阶段：

1. 增加 `remote` sink；
2. 调用远程 `/api/accounts`；
3. 增加失败备份；
4. 增加通过 `register.replay_failed_path` 控制的失败重放。

第三阶段：

1. 增加统计汇总；
2. 增加配置模板 `register_external.example.toml`；
3. 增加远程 `/api/accounts` 鉴权自检。

## 验收标准

脚本完成后至少要满足：

1. 不传参数时默认读取同目录 `register_external.example.toml`；
2. 配置文件缺失时给出明确错误；
3. `mail.providers` 为空时给出明确错误；
4. 注册成功后不会修改当前项目 `data/accounts.json`；
5. `jsonl` sink 能写出完整账号三件套；
6. `remote` sink 能把账号导入远程部署的本项目；
7. 远程导入失败时，本地有可重放备份；
8. 日志不泄露完整 token 和 key。
