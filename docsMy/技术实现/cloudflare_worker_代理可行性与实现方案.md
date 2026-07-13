# Cloudflare Worker 作为项目代理的可行性与实现方案

## 结论

不建议把 Cloudflare Worker 当成当前项目 `proxy` 字段的直接替代品。

本项目现在的代理配置本质上是给 `curl_cffi.requests.Session(proxy=...)` 使用的 HTTP / HTTPS / SOCKS5 / SOCKS5H 出站代理地址。也就是说，代码期望代理服务端支持标准 HTTP `CONNECT` 隧道，或者支持 SOCKS 协议。

Cloudflare Worker 暴露给外部的是 HTTP / WebSocket 请求处理入口，不是标准 HTTP forward proxy，也不是 SOCKS 服务。Worker 虽然提供 `fetch()` 发起 HTTP 子请求，也提供 `connect()` 发起出站 TCP 连接，但 `connect()` 是 Worker 到上游的出站 TCP 能力，并不能让 Worker 的公开 URL 直接变成一个可填入本项目 `proxy` 字段的 HTTP/SOCKS 代理地址。

因此：

| 方案 | 是否可直接填入注册页“注册代理” | 是否建议 |
|------|-------------------------------|----------|
| `https://xxx.workers.dev` 直接填到 `proxy` | 不可行 | 不建议 |
| Worker 实现受限 HTTP Relay，本项目改请求层适配 | 理论可行 | 只适合简单 API 请求，不适合完整注册链路 |
| Worker + WebSocket/TCP 隧道 + 本地适配器 | 理论可做 | 复杂、脆弱，不建议 |
| VPS / 自建 HTTP CONNECT 或 SOCKS 代理 | 可行 | 更适合当前项目 |

## 当前项目的代理使用方式

### 全局代理

`services/proxy_service.py` 会从账号代理、显式传入代理、全局代理配置中选择一个字符串，然后传给 `curl_cffi.requests.Session`：

```python
session_kwargs["proxy"] = proxy
```

支持的格式由项目校验逻辑限定为：

```text
http://host:port
https://host:port
socks5://host:port
socks5h://host:port
```

### 注册机代理

注册页的“注册代理”字段保存到 `data/register.json` 的 `proxy` 字段。启动注册任务时，后端会把这个代理同步给注册请求和邮箱 provider：

```python
openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
```

注册流程创建 Session 时直接使用：

```python
requests.Session(impersonate="chrome", verify=False, proxy=proxy)
```

邮箱 provider 也会读取 `mail.proxy`，再创建自己的 `requests.Session(proxy=...)`。

这说明当前 `proxy` 字段不是“转发 URL”，而是标准代理协议地址。

## 为什么 Worker 不能直接作为当前代理

### 1. Worker URL 不是 HTTP CONNECT 代理

当前项目访问 HTTPS 上游时，HTTP 代理需要支持类似下面的流程：

```text
CONNECT auth.openai.com:443 HTTP/1.1
Host: auth.openai.com:443

<代理建立 TCP 隧道>
<客户端在隧道内发起 TLS 握手>
```

普通 Worker 的 `fetch(request)` 处理的是 HTTP 请求和响应，并不是一个标准 forward proxy 服务。把 `https://xxx.workers.dev` 填入 `proxy` 后，`curl_cffi` 会把它当成代理端点，然后尝试发起 `CONNECT` 隧道。Worker 侧并不能按标准代理服务器方式接管任意 TLS 隧道。

之前遇到的错误：

```text
curl: (56) CONNECT tunnel failed, response 400
```

就是典型的“客户端以为对方是 HTTP CONNECT 代理，但对方返回了普通 HTTP 错误”的表现。

### 2. Worker 的 `connect()` 是出站能力

Cloudflare Workers 的 TCP Socket API 可以让 Worker 从边缘节点主动连接某个 TCP 目标，例如数据库、SMTP、SSH 等。这是 Worker 到上游的连接能力，不等同于对外暴露一个通用 TCP 代理端口。

即使 Worker 内部可以 `connect({ hostname, port })`，本项目的 `curl_cffi` 客户端仍然需要一个标准 HTTP/SOCKS 代理入口。Worker 默认入口是 HTTP 请求，不是 SOCKS，也不是 raw TCP listener。

### 3. 即便改成 Relay，也会丢失关键网络特征

如果把项目改成“请求 Worker，Worker 再 fetch 上游”，链路会变成：

```text
chatgpt2api 后端 -> Worker /relay -> Worker fetch(auth.openai.com)
```

这不再是本机 `curl_cffi` 直接访问上游，很多行为会变：

- TLS 指纹变成 Cloudflare Worker 的上游请求特征；
- `curl_cffi` 的 `impersonate="chrome"` 不再作用于 Worker 到上游这一段；
- Cookie、重定向、`Set-Cookie`、二进制 body、压缩、流式响应都要重新封装；
- Worker 出口 IP 是 Cloudflare 网络，可能被上游风控识别；
- 注册链路依赖 Auth0 / OpenAI 多步跳转和验证码邮件，Relay 适配成本高。

所以 Worker Relay 更适合简单、明确、可控的 HTTP API 请求，不适合拿来承载完整浏览器风格注册链路。

## 可选方案一：不改代码，使用真正的 HTTP/SOCKS 代理

这是当前项目最匹配的方案。

架构：

```text
chatgpt2api 后端
  -> http://proxy-host:port 或 socks5h://proxy-host:port
  -> 上游服务
```

优点：

- 不需要改项目代码；
- 兼容 `curl_cffi` 的 `proxy` 参数；
- 注册流程、邮箱 provider、图片请求都能复用现有实现；
- 保留客户端侧 TLS/HTTP 行为控制能力。

注意：

- 如果项目跑在 Docker 内，`127.0.0.1` 指向容器内部，不是宿主机；
- 本机代理给 Docker 用时，通常需要填 `http://host.docker.internal:7890`；
- 代理服务必须支持 HTTPS `CONNECT`；
- 不建议使用公开免费代理，稳定性和安全性都很差。

## 可选方案二：Worker 受限 HTTP Relay

这个方案不是把 Worker 填入 `proxy` 字段，而是给项目新增一种独立传输模式。

适用范围：

- 简单 GET / POST API；
- 请求体较小；
- 不依赖浏览器 TLS 指纹；
- 不依赖复杂 Cookie jar；
- 不需要长连接或稳定流式响应。

不适合：

- 注册机完整链路；
- ChatGPT Web 复杂会话链路；
- 大文件上传；
- SSE 长流；
- 需要强浏览器指纹一致性的请求。

### 目标架构

```text
chatgpt2api
  -> POST https://relay.example.com/relay
      {
        "method": "GET",
        "url": "https://example.com/path",
        "headers": {},
        "body_base64": ""
      }
  -> Cloudflare Worker 校验、过滤、fetch 上游
  <- {
       "status": 200,
       "headers": {},
       "body_base64": "...",
       "final_url": "https://example.com/path"
     }
```

### Worker 侧要求

Worker 必须是受限 Relay，不能做开放代理。

必要限制：

- 只允许 `POST /relay`；
- 必须使用 `Authorization: Bearer <relay-token>` 或 HMAC 签名；
- 只允许访问白名单域名；
- 只允许 `http` / `https`；
- 禁止访问内网地址、localhost、metadata IP；
- 限制请求体大小；
- 限制响应体大小；
- 移除 hop-by-hop headers；
- 禁止客户端传入 `Host`、`Connection`、`Proxy-*` 等敏感头；
- 日志中不记录 Authorization、Cookie、验证码、token。

建议白名单按用途拆开：

```text
auth.openai.com
platform.openai.com
chatgpt.com
api.openai.com
<已配置的邮箱 provider API 域名>
```

### 项目侧改造点

新增配置：

```json
{
  "relay": {
    "enabled": false,
    "url": "https://relay.example.com/relay",
    "token": "",
    "allow_register": false,
    "allow_mail": true
  }
}
```

新增后端模块建议：

```text
services/relay_transport.py
```

职责：

- 统一封装 Relay 请求；
- 将 method、url、headers、body 编码成 JSON；
- 处理 Worker 返回的 status、headers、body；
- 做超时、重试、错误转换；
- 严格过滤敏感 header。

建议先只接邮箱 provider，不接注册链路：

```text
services/register/mail_provider.py
  -> 对简单邮箱 API 请求支持 relay_transport

services/register/openai_register.py
  -> 暂不接 relay_transport，继续要求标准 HTTP/SOCKS 代理
```

原因是邮箱 API 大多是普通 JSON API，Relay 成本较低；注册链路依赖多步认证、Cookie、浏览器指纹，接入 Relay 后失败率和维护成本都很高。

### Worker Relay 伪代码

下面是结构示意，不建议直接作为生产开放代理使用：

```ts
export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    const auth = request.headers.get("authorization") || "";
    if (auth !== `Bearer ${env.RELAY_TOKEN}`) {
      return new Response("unauthorized", { status: 401 });
    }

    const payload = await request.json();
    const target = new URL(payload.url);

    if (!["https:", "http:"].includes(target.protocol)) {
      return new Response("bad protocol", { status: 400 });
    }

    if (!isAllowedHost(target.hostname, env.ALLOW_HOSTS)) {
      return new Response("host not allowed", { status: 403 });
    }

    const headers = sanitizeHeaders(payload.headers || {});
    const body = payload.body_base64
      ? Uint8Array.from(atob(payload.body_base64), (c) => c.charCodeAt(0))
      : undefined;

    const upstream = await fetch(target.toString(), {
      method: payload.method || "GET",
      headers,
      body,
      redirect: "manual",
    });

    const bytes = new Uint8Array(await upstream.arrayBuffer());

    return Response.json({
      status: upstream.status,
      headers: pickResponseHeaders(upstream.headers),
      body_base64: btoa(String.fromCharCode(...bytes)),
      final_url: upstream.url,
    });
  },
};
```

生产实现必须补齐：

- `isAllowedHost`；
- `sanitizeHeaders`；
- `pickResponseHeaders`；
- body 大小限制；
- 响应大小限制；
- 请求超时；
- 结构化错误返回；
- 日志脱敏。

## 可选方案三：Worker + WebSocket 隧道 + 本地适配器

理论上可以做：

```text
curl_cffi -> 本地 HTTP CONNECT 适配器 -> WebSocket -> Worker -> connect() -> 上游
```

但这已经不是“Worker 代理地址”，而是一套自定义隧道系统。项目仍然需要本地适配器提供标准 HTTP/SOCKS 入口。

问题：

- 本地仍然要跑一个代理适配器；
- WebSocket 双向流和 TCP half-open 处理复杂；
- 错误恢复、超时、TLS 握手、并发连接都要自己维护；
- Worker 每次 invocation 的连接限制会影响并发；
- 成本和复杂度高于直接使用 VPS 代理。

不建议为本项目走这条路线。

## 推荐落地路径

### 短期

维持当前 `proxy` 设计，使用真正支持 HTTP `CONNECT` 的代理。

如果只是解决本地清空代理后仍报：

```text
CONNECT tunnel failed, response 400
```

优先检查：

1. `data/register.json` 的 `proxy` 是否为空；
2. `data/register.json` 的 `mail.proxy` 是否残留旧代理；
3. 后端进程是否重启；
4. 运行后端的 shell / Docker 容器内是否有 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 环境变量；
5. 注册页是否重新保存配置后再启动。

### 中期

如果确实需要 Worker，建议只做“邮箱 provider Relay”：

1. 新增 `services/relay_transport.py`；
2. 新增系统配置 `relay.enabled`、`relay.url`、`relay.token`；
3. 在邮箱 provider 的 `_request` 封装里可选走 Relay；
4. 注册 OpenAI/Auth0 主链路仍使用标准代理；
5. 前端设置页新增 Relay 配置和测试按钮。

### 长期

抽象统一出站请求层：

```text
services/outbound_transport.py
```

提供三种模式：

```text
direct        直连
proxy         HTTP/SOCKS 代理
worker_relay 受限 Worker Relay
```

但需要注意，`curl_cffi` 的浏览器指纹能力和 Relay 模式天然冲突。凡是依赖浏览器指纹、Cookie jar、复杂跳转的链路，应继续保留 direct/proxy 模式。

## 安全边界

Worker Relay 绝不能做成公开通用代理，否则会带来严重风险：

- 被第三方滥用；
- Cloudflare 账号被风控或封禁；
- 上游 token、Cookie、验证码泄露；
- SSRF 访问内网或云厂商 metadata；
- 日志中泄露敏感数据。

因此实现时必须坚持：

- 默认关闭；
- 强认证；
- 强白名单；
- 严格限流；
- 日志脱敏；
- 禁止开放代理行为。

## 参考资料

- Cloudflare Workers Fetch API：`https://developers.cloudflare.com/workers/runtime-apis/fetch/`
- Cloudflare Workers TCP sockets：`https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/`
- Cloudflare Workers Limits：`https://developers.cloudflare.com/workers/platform/limits/`
- Cloudflare Connection limits：`https://developers.cloudflare.com/fundamentals/reference/connection-limits/`

