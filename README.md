# Codex Session Relay

## 我打下广告 提供稳定的优质ai资源
牛爷爷的AI小站:https://pay.ldxp.cn/shop/EIPG9I9L  小猫GPT源头:https://pay.ldxp.cn/shop/1D0LD6BR

感谢关注, 谢谢喵

## 介绍
Codex Session Relay 是一个本地代理工具，把浏览器里的 ChatGPT 登录态导入到本机，可让 Codex CLI 通过本地地址转发请求到 ChatGPT Codex。

主要功能：

- 导入 ChatGPT `api/auth/session` 中的 `accessToken`。
- 本地代理 Codex CLI 的 Responses 请求。
- 支持多账号保存、切换和删除。
- 显示 Codex 5 小时和 7 天限额。
- 生成 Codex CLI 配置，也支持导入 CC-Switch。

脚本只依赖 Python 标准库，默认监听：

```text
http://127.0.0.1:8765/backend-api/codex
```

## 使用方法

在项目目录运行：

```powershell
python .\codex_session_relay.py
```

或者双击exe

启动后会打开桌面窗口。按下面步骤使用：

1. 在浏览器打开 `https://chatgpt.com/api/auth/session`。
2. 复制页面返回的完整 JSON。
3. 粘贴到 Relay 窗口的 session 输入框。
4. 点击“导入”。
5. 按窗口中展示的内容配置 Codex CLI。

如果不需要桌面窗口，可以使用后台模式：

```powershell
python .\codex_session_relay.py --no-gui
```

如果默认端口被占用，可以指定端口：

```powershell
python .\codex_session_relay.py --port 9000
```

## Codex CLI 配置

`config.toml` 示例：

```toml
model_provider = "local_codex_relay"
disable_response_storage = true

[model_providers]
[model_providers.local_codex_relay]
name = "local_codex_relay"
base_url = "http://127.0.0.1:8765/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
```

`auth.json` 示例：

```json
{
  "OPENAI_API_KEY": "local-codex-relay"
}
```

这里的 `OPENAI_API_KEY` 只是本地占位值，真正用于访问上游的是导入到 Relay 的 ChatGPT `accessToken`。

## 注意事项

- 需要 Python 3.10 或更高版本。
- 网络需要能访问 `chatgpt.com`。
- `relay_store.json` 会保存敏感 token，不要提交到 Git，也不要发送给别人。
- 这个工具设计为本机使用，默认只监听 `127.0.0.1`，不要暴露到公网或不可信局域网。
