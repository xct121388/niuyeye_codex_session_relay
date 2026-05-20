# Codex Session Relay

Codex Session Relay 是一个本地代理工具，用于把 ChatGPT `api/auth/session` 中的 `accessToken` 导入到本机，然后把 Codex CLI 的 Responses 请求转发到 ChatGPT Codex 上游。

脚本只依赖 Python 标准库，默认监听 `127.0.0.1:8765`，并提供一个 Tkinter 桌面窗口用于导入账号、切换账号、查看限额和生成配置。

## 设计思路

这个工具的核心目标是把“浏览器里的 ChatGPT 登录态”和“本地 Codex CLI 请求”连接起来。

整体流程如下：

1. 用户在浏览器打开 `https://chatgpt.com/api/auth/session`，复制完整 JSON。
2. 本地 Relay 解析 JSON，提取 `accessToken`、过期时间、用户信息和账号信息。
3. Codex CLI 请求本地 Relay，例如 `http://127.0.0.1:8765/backend-api/codex/responses`。
4. Relay 用当前激活账号的 `accessToken` 构造上游请求头。
5. Relay 把请求转发到 `https://chatgpt.com/backend-api/codex/responses`，并把流式响应原样返回给 Codex CLI。

代码结构上，脚本主要分成几层：

- `RelayState`：管理多账号 session、当前激活账号、最近请求日志和 Codex 限额快照。
- `CodexRelayHandler`：处理本地 HTTP API 和 Codex 请求转发。
- `RelayDesktopApp`：提供桌面窗口，负责导入 session、展示状态、切换账号、打开设置和导入 CC-Switch。
- 存储相关函数：把账号、端口、关闭行为等配置统一保存到 `relay_store.json`。

## 功能

- 导入 ChatGPT `api/auth/session` JSON。
- 支持多账号保存、切换、编辑和删除。
- 自动检查 `accessToken` 是否过期。
- 代理 Codex CLI Responses 请求。
- 支持 `/backend-api/codex/responses` 和 `/v1/responses` 两类本地路径。
- 转发 Codex 流式响应。
- 记录最近 20 条代理请求。
- 解析 Codex 限额响应头，展示 5 小时和 7 天窗口用量。
- 提供 Codex CLI `config.toml` 和 `auth.json` 示例。
- 支持一键导入 CC-Switch。
- 支持修改监听端口。
- Windows 下支持设置当前用户开机启动。

## 环境要求

- Python 3.10 或更高版本。
- Windows 桌面环境可使用完整 GUI。
- 仅命令行运行时可使用 `--no-gui`。
- 网络需要能访问 `chatgpt.com`。

脚本不需要额外安装第三方 Python 包。

## 快速开始

在项目目录运行：

```powershell
python .\codex_session_relay.py
```

启动后会打开桌面窗口，并在终端输出类似：

```text
[relay] Codex Session Relay 已启动
[relay] 桌面窗口: 开启
[relay] 推荐 base_url: http://127.0.0.1:8765/backend-api/codex
[relay] 按 Ctrl+C 停止
```

然后按下面步骤使用：

1. 点击窗口里的“获取 auth session”，或手动打开 `https://chatgpt.com/api/auth/session`。
2. 复制页面返回的完整 JSON。
3. 粘贴到窗口的 session 输入框。
4. 点击“导入”。
5. 按窗口中展示的配置修改 Codex CLI 配置。

## Codex CLI 配置

默认本地地址是：

```text
http://127.0.0.1:8765/backend-api/codex
```

`config.toml` 示例：

```toml
model = "gpt-5.5"

[model_providers.openai]
name = "local-codex-relay"
base_url = "http://127.0.0.1:8765/backend-api/codex"
wire_api = "responses"
```

`auth.json` 示例：

```json
{
  "OPENAI_API_KEY": "local-codex-relay"
}
```

这里的 `OPENAI_API_KEY` 只是给本地 Codex CLI 通过认证字段使用的占位值。真正用于访问上游的是导入到 Relay 的 ChatGPT `accessToken`。

## CC-Switch 使用

如果本机安装了 CC-Switch，可以在桌面窗口点击“导入 CC-Switch”。

Relay 会生成一个 `ccswitch://v1/import` 链接，内容包括：

- 应用类型：`codex`
- 模型：`gpt-5.5`
- endpoint：`http://127.0.0.1:8765/backend-api/codex`
- apiKey：`local-codex-relay`
- 用量脚本：从 `/api/status` 读取 5 小时和 7 天限额

## 命令行参数

```powershell
python .\codex_session_relay.py [参数]
```

可用参数：

| 参数 | 说明 |
| --- | --- |
| `--host` | 监听地址，默认 `127.0.0.1` |
| `--port` | 监听端口；不传时读取设置文件，默认 `8765` |
| `--store` | 指定存储文件路径，默认脚本同目录 `relay_store.json` |
| `--settings` | 兼容旧参数，当前等同于未指定 `--store` 时的存储路径覆盖 |
| `--no-gui` | 不启动桌面窗口，只运行本地代理服务 |

示例：

```powershell
python .\codex_session_relay.py --port 9000
```

仅后台代理：

```powershell
python .\codex_session_relay.py --no-gui
```

## 本地 API

### `GET /api/status`

返回当前状态，包括：

- 是否已导入 session。
- 当前账号的非敏感信息。
- 多账号列表。
- 当前激活账号 key。
- Codex 限额快照。
- 最近代理请求日志。
- Codex CLI 配置示例。

### `POST /api/import-session`

导入 session。

请求体可以是完整 `api/auth/session` JSON，也可以是：

```json
{
  "content": "{\"accessToken\":\"...\",\"expires\":\"...\"}"
}
```

### `POST /api/switch-session`

切换当前激活账号。

```json
{
  "key": "user_id:..."
}
```

### `POST /api/clear-session`

删除当前激活账号。

### `GET /api/refresh-usage`

主动向上游发送一个最小 Codex 请求，用于刷新当前账号的 Codex 限额响应头。

### `POST /backend-api/codex/responses`

Codex 代理入口，会转发到：

```text
https://chatgpt.com/backend-api/codex/responses
```

也支持带后缀路径：

```text
/backend-api/codex/responses/...
/v1/responses
/v1/responses/...
```

## 数据存储

默认存储文件是脚本同目录下的：

```text
relay_store.json
```

其中会保存：

- 已导入账号的 `access_token`。
- 账号邮箱、用户 ID、账号 ID、套餐类型。
- token 指纹。
- 当前激活账号。
- 端口、关闭行为等设置。

注意：`relay_store.json` 包含敏感 token，不要提交到 Git，也不要发送给别人。

脚本会自动兼容旧文件：

- `session_store.json`
- `relay_settings.json`

如果新的 `relay_store.json` 不存在，启动时会尝试把旧账号和旧设置合并进去。

## 请求转发规则

Relay 只转发 Codex 请求需要的部分请求头，包括：

- `user-agent`
- `session_id`
- `conversation_id`
- `x-codex-turn-state`
- `x-codex-turn-metadata`
- `content-type`
- `accept-language`

同时会补充或覆盖上游需要的请求头：

- `Authorization: Bearer <accessToken>`
- `OpenAI-Beta: responses=experimental`
- `originator: codex_cli_rs`
- `Accept: text/event-stream`
- `Content-Type: application/json`
- `User-Agent: codex_cli_rs/0.125.0`

响应返回时会丢弃连接级响应头，例如 `transfer-encoding`、`content-length`、`content-encoding` 等，避免本地代理响应和上游响应的连接语义冲突。

## 常见问题

### 提示 `请先导入 api/auth/session`

当前没有可用账号。打开桌面窗口导入新的 `api/auth/session` JSON。

### 提示 `accessToken 已过期`

浏览器里的 ChatGPT session 已过期或本地保存的 token 已失效。重新打开 `https://chatgpt.com/api/auth/session`，复制新的 JSON 后导入。

### 端口被占用

可以在设置窗口修改端口，或命令行指定：

```powershell
python .\codex_session_relay.py --port 9000
```

修改端口后，Codex CLI 的 `base_url` 也要同步改成新端口。

### 没有桌面环境

使用无界面模式：

```powershell
python .\codex_session_relay.py --no-gui
```

然后通过 `/api/import-session` 导入账号，通过 `/api/status` 查看状态。

## 安全说明

这个工具设计为本机使用，默认只监听 `127.0.0.1`。

不要把服务暴露到公网或局域网不可信环境。Relay 保存并使用 ChatGPT `accessToken`，一旦泄露，别人可能在 token 有效期内以你的账号访问相关服务。
