#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Codex Session Relay。

该脚本只依赖 Python 标准库，用于在本机导入一个 ChatGPT `api/auth/session`
结果，并把 Codex CLI 的 Responses 请求转发到 ChatGPT Codex 上游。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import posixpath
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

if sys.platform.startswith("win"):
    import winreg
else:
    winreg = None  # type: ignore[assignment]


UPSTREAM_CODEX_RESPONSES = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CC_SWITCH_CODEX_MODEL = "gpt-5.5"
LOCAL_API_KEY = "local-codex-relay"
MAX_BODY_BYTES = 64 * 1024 * 1024
STREAM_CHUNK_SIZE = 64 * 1024
RECENT_LOG_LIMIT = 20
CODEX_PROBE_TIMEOUT = 20
STORE_FILE = "relay_store.json"
LEGACY_SESSION_FILE = "session_store.json"
LEGACY_SETTINGS_FILE = "relay_settings.json"
STARTUP_RUN_NAME = "CodexSessionRelay"
CLOSE_ACTION_EXIT = "exit"
CLOSE_ACTION_MINIMIZE = "minimize"
ICON_ICO_FILES = ("logo.ico", "app.ico", "codex_session_relay.ico")
ICON_IMAGE_FILES = ("logo.png", "app.png", "codex_session_relay.png")

PASS_REQUEST_HEADERS = {
    "user-agent",
    "session_id",
    "conversation_id",
    "x-codex-turn-state",
    "x-codex-turn-metadata",
    "content-type",
    "accept-language",
}

DROP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


class RelayState:
    """保存多账号 session、当前激活账号和最近请求日志。"""

    def __init__(self, store_path: str) -> None:
        self.store_path = store_path
        self.lock = threading.RLock()
        self.sessions: list[dict[str, Any]] = []
        self.active_session_key = ""
        self.logs: list[dict[str, Any]] = []
        self.codex_usage: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """从本地状态文件加载 session，并兼容旧单账号格式。"""
        if not os.path.exists(self.store_path):
            return
        try:
            self.load_from_data(load_store_file(self.store_path))
        except Exception as exc:
            print(f"[relay] 状态文件加载失败: {exc}", file=sys.stderr)

    def save(self) -> None:
        """保存当前账号列表到本地状态文件。"""
        with self.lock:
            data = {
                **load_store_file(self.store_path),
                "version": 3,
                "active_session_key": self.active_session_key,
                "sessions": [session_for_store(session) for session in self.sessions],
            }
            save_store_file(self.store_path, data)

    def load_from_data(self, data: Any) -> None:
        """从统一存储或旧单账号格式载入账号。"""
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            self.sessions = [normalize_stored_session(item) for item in data["sessions"] if isinstance(item, dict) and item.get("access_token")]
            self.active_session_key = string_value(data.get("active_session_key"))
        elif isinstance(data, dict) and data.get("access_token"):
            session = normalize_stored_session(data)
            self.sessions = [session]
            self.active_session_key = session_identity_key(session)
        if self.sessions and not self.find_session(self.active_session_key):
            self.active_session_key = session_identity_key(self.sessions[0])

    def import_session(self, raw: Any) -> dict[str, Any]:
        """导入 api/auth/session 内容。"""
        record = build_session_record(raw)
        key = session_identity_key(record)
        with self.lock:
            old = self.find_session(key)
            if old is not None:
                self.sessions[self.sessions.index(old)] = record
            else:
                self.sessions.append(record)
            self.active_session_key = key
            self.save()
        return public_session(record)

    def update_session(self, key: str, raw: Any) -> dict[str, Any]:
        """更新指定账号的 session。"""
        record = build_session_record(raw)
        new_key = session_identity_key(record)
        if key != new_key:
            raise ValueError("编辑内容不是当前选中账号的 session")
        with self.lock:
            old = self.find_session(key)
            if old is None:
                raise ValueError("账号不存在")
            self.sessions[self.sessions.index(old)] = record
            self.active_session_key = key
            self.save()
        return public_session(record)

    def delete_session(self, key: str) -> bool:
        """删除指定账号。"""
        with self.lock:
            session = self.find_session(key)
            if session is None:
                return False
            self.sessions.remove(session)
            if self.active_session_key == key:
                self.active_session_key = session_identity_key(self.sessions[0]) if self.sessions else ""
            self.save()
            return True

    def clear_session(self) -> None:
        """清空当前激活 session。"""
        self.delete_session(self.active_session_key)

    def get_session(self) -> dict[str, Any] | None:
        """返回当前 session 副本。"""
        with self.lock:
            session = self.find_session(self.active_session_key)
            return dict(session) if session else None

    def set_active_session(self, key: str) -> bool:
        """切换当前激活账号。"""
        with self.lock:
            if self.find_session(key) is None:
                return False
            self.active_session_key = key
            self.save()
            return True

    def find_session(self, key: str) -> dict[str, Any] | None:
        """按账号身份查找 session。"""
        for session in self.sessions:
            if session_identity_key(session) == key:
                return session
        return None

    def public_sessions(self) -> list[dict[str, Any]]:
        """返回账号列表的非敏感信息。"""
        with self.lock:
            return [public_session(item) for item in self.sessions]

    def export_session_json(self, key: str) -> str:
        """导出指定账号当前保存的 session JSON。"""
        with self.lock:
            session = self.find_session(key)
            if session is None:
                return ""
            return json.dumps(session_to_auth_payload(session), ensure_ascii=False, indent=2)

    def add_log(self, entry: dict[str, Any]) -> None:
        """记录一次代理请求。"""
        with self.lock:
            self.logs.insert(0, entry)
            del self.logs[RECENT_LOG_LIMIT:]

    def update_codex_usage(self, headers: Any) -> None:
        """保存最近一次 Codex 限额快照。"""
        usage = parse_codex_rate_limit_headers(headers)
        if not usage:
            return
        with self.lock:
            session = self.find_session(self.active_session_key)
            if session is None:
                return
            self.codex_usage[session_identity_key(session)] = usage

    def refresh_codex_usage(self) -> dict[str, Any]:
        """主动请求上游刷新当前账号的 Codex 限额。"""
        session = self.get_session()
        if not session:
            raise ValueError("请先导入 api/auth/session")
        public = public_session(session)
        if public and public.get("expired"):
            raise ValueError("accessToken 已过期，请重新导入 api/auth/session")
        headers = probe_codex_usage_headers(session)
        usage = parse_codex_rate_limit_headers(headers)
        if not usage:
            raise ValueError("上游响应未返回 Codex 限额头")
        with self.lock:
            active = self.find_session(self.active_session_key)
            if active is None:
                raise ValueError("当前账号不存在")
            self.codex_usage[session_identity_key(active)] = usage
        return usage

    def status(self, base_url: str) -> dict[str, Any]:
        """返回前端状态数据。"""
        with self.lock:
            active = self.find_session(self.active_session_key)
            session = public_session(active) if active else None
            codex_usage = public_codex_usage(self.codex_usage.get(self.active_session_key, {})) if active else {}
            sessions = [public_session(item) for item in self.sessions]
            logs = [dict(item) for item in self.logs]
        return {
            "ok": True,
            "has_session": session is not None,
            "session": session,
            "sessions": sessions,
            "active_session_key": self.active_session_key,
            "codex_usage": codex_usage,
            "logs": logs,
            "config": build_config_examples(base_url),
            "server_time": format_datetime(datetime.now(timezone.utc)),
        }


def build_session_record(raw: Any) -> dict[str, Any]:
    """解析导入内容并生成 session 记录。"""
    payload = normalize_import_payload(raw)
    token = first_string(payload, ("accessToken",), ("access_token",), ("tokens", "access_token"), ("tokens", "accessToken"))
    if not token:
        raise ValueError("缺少 accessToken")

    expires_raw = first_value(payload, ("expires",), ("expires_at",), ("expiresAt",), ("tokens", "expires_at"), ("tokens", "expiresAt"))
    expires_at = parse_time_value(expires_raw)
    if expires_at is None:
        raise ValueError("缺少或无法解析 expires")
    now = datetime.now(timezone.utc)
    if expires_at <= now:
        raise ValueError(f"accessToken 已过期: {format_datetime(expires_at)}")

    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    record = {
        "access_token": token,
        "expires": format_datetime(expires_at),
        "email": string_value(user.get("email")),
        "user_id": string_value(user.get("id")),
        "account_id": string_value(account.get("id")),
        "plan_type": string_value(account.get("planType") or account.get("plan_type")),
        "token_sha256": token_hash,
        "token_fingerprint": token_hash[:12],
        "session_token_present": bool(first_string(payload, ("sessionToken",), ("session_token",))),
        "imported_at": format_datetime(now),
    }
    return record


def session_to_auth_payload(session: dict[str, Any]) -> dict[str, Any]:
    """把内部记录还原成可编辑的 session JSON。"""
    payload: dict[str, Any] = {
        "accessToken": session.get("access_token", ""),
        "expires": session.get("expires", ""),
        "user": {},
        "account": {},
    }
    if session.get("email"):
        payload["user"]["email"] = session["email"]
    if session.get("user_id"):
        payload["user"]["id"] = session["user_id"]
    if session.get("account_id"):
        payload["account"]["id"] = session["account_id"]
    if session.get("plan_type"):
        payload["account"]["planType"] = session["plan_type"]
    return payload


def normalize_import_payload(raw: Any) -> dict[str, Any]:
    """把导入请求归一化为 session 对象。"""
    if isinstance(raw, dict) and isinstance(raw.get("content"), str):
        content = raw["content"].strip()
        if not content:
            raise ValueError("content 为空")
        decoded = json.loads(content)
        if not isinstance(decoded, dict):
            raise ValueError("content 必须是 JSON 对象")
        return decoded
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("导入内容必须是 JSON 对象")
        return decoded
    raise ValueError("导入内容格式不支持")


def first_value(obj: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    """按路径读取第一个存在的值。"""
    for path in paths:
        current: Any = obj
        found = True
        for key in path:
            if not isinstance(current, dict) or key not in current:
                found = False
                break
            current = current[key]
        if found:
            return current
    return None


def first_string(obj: dict[str, Any], *paths: tuple[str, ...]) -> str:
    """按路径读取第一个非空字符串。"""
    for path in paths:
        value = string_value(first_value(obj, path))
        if value:
            return value
    return ""


def string_value(value: Any) -> str:
    """把简单值转换为字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def normalize_stored_session(session: dict[str, Any]) -> dict[str, Any]:
    """补齐历史 session 记录的必要字段。"""
    session = dict(session)
    session.pop("codex_usage", None)
    return session


def session_for_store(session: dict[str, Any]) -> dict[str, Any]:
    """生成可落盘的账号数据。"""
    stored = dict(session)
    stored.pop("codex_usage", None)
    return stored


def session_identity_key(session: dict[str, Any] | None) -> str:
    """生成判断是否同一用户的账号身份。"""
    if not session:
        return ""
    for key in ("user_id", "account_id", "email"):
        value = string_value(session.get(key))
        if value:
            return f"{key}:{value.lower()}"
    return "token:" + string_value(session.get("token_sha256"))


def session_display_name(session: dict[str, Any] | None) -> str:
    """生成账号下拉框显示名称。"""
    if not session:
        return "未导入账号"
    email = string_value(session.get("email"))
    plan = string_value(session.get("plan_type"))
    if email and plan:
        return f"{email} ({plan})"
    return email or string_value(session.get("user_id")) or string_value(session.get("account_id")) or "未知账号"


def parse_time_value(value: Any) -> datetime | None:
    """解析 session 过期时间。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return unix_to_datetime(float(value))
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.isdigit():
        return unix_to_datetime(float(raw))
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def unix_to_datetime(value: float) -> datetime:
    """把秒或毫秒时间戳转换为 UTC 时间。"""
    if value > 1_000_000_000_000:
        value = value / 1000
    return datetime.fromtimestamp(value, tz=timezone.utc)


def format_datetime(value: datetime) -> str:
    """格式化 UTC 时间。"""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def public_session(session: dict[str, Any] | None) -> dict[str, Any] | None:
    """返回不含敏感 token 的 session 信息。"""
    if not session:
        return None
    expires = parse_time_value(session.get("expires"))
    remaining_seconds = None
    expired = False
    if expires is not None:
        remaining_seconds = int((expires - datetime.now(timezone.utc)).total_seconds())
        expired = remaining_seconds <= 0
    return {
        "key": session_identity_key(session),
        "display_name": session_display_name(session),
        "email": session.get("email", ""),
        "user_id": session.get("user_id", ""),
        "account_id": session.get("account_id", ""),
        "plan_type": session.get("plan_type", ""),
        "expires": session.get("expires", ""),
        "remaining_seconds": remaining_seconds,
        "expired": expired,
        "token_fingerprint": session.get("token_fingerprint", ""),
        "session_token_present": bool(session.get("session_token_present")),
        "imported_at": session.get("imported_at", ""),
    }


def probe_codex_usage_headers(session: dict[str, Any]) -> Any:
    """用最小 Codex 请求主动获取上游限额响应头。"""
    payload = {
        "model": CC_SWITCH_CODEX_MODEL,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
        "stream": True,
        "store": False,
        "instructions": "You are Codex, a coding agent.",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + session["access_token"],
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "Originator": "codex_cli_rs",
        "Version": "0.125.0",
        "User-Agent": "codex_cli_rs/0.125.0",
    }
    account_id = string_value(session.get("account_id"))
    if account_id:
        headers["chatgpt-account-id"] = account_id
    req = urllib.request.Request(UPSTREAM_CODEX_RESPONSES, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=CODEX_PROBE_TIMEOUT) as resp:
            return resp.headers
    except urllib.error.HTTPError as exc:
        return exc.headers


def parse_codex_rate_limit_headers(headers: Any) -> dict[str, Any]:
    """从 Codex 响应头解析并归一化限额信息。"""
    primary = {
        "used_percent": parse_header_float(headers, "x-codex-primary-used-percent"),
        "reset_after_seconds": parse_header_int(headers, "x-codex-primary-reset-after-seconds"),
        "window_minutes": parse_header_int(headers, "x-codex-primary-window-minutes"),
    }
    secondary = {
        "used_percent": parse_header_float(headers, "x-codex-secondary-used-percent"),
        "reset_after_seconds": parse_header_int(headers, "x-codex-secondary-reset-after-seconds"),
        "window_minutes": parse_header_int(headers, "x-codex-secondary-window-minutes"),
    }
    overflow = parse_header_float(headers, "x-codex-primary-over-secondary-limit-percent")
    if not any(value is not None for value in [*primary.values(), *secondary.values(), overflow]):
        return {}

    used5h_from_primary, used7d_from_primary = codex_limit_mapping(primary["window_minutes"], secondary["window_minutes"])
    if used5h_from_primary:
        limit5h, limit7d = primary, secondary
    elif used7d_from_primary:
        limit5h, limit7d = secondary, primary
    else:
        limit5h, limit7d = {}, {}

    updated_at = format_datetime(datetime.now(timezone.utc))
    return {
        "updated_at": updated_at,
        "primary": compact_none(primary),
        "secondary": compact_none(secondary),
        "primary_over_secondary_percent": overflow,
        "limit_5h": build_limit_record(limit5h, updated_at),
        "limit_7d": build_limit_record(limit7d, updated_at),
    }


def parse_header_float(headers: Any, key: str) -> float | None:
    """解析浮点响应头。"""
    value = header_value(headers, key)
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_header_int(headers: Any, key: str) -> int | None:
    """解析整数响应头。"""
    value = header_value(headers, key)
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def header_value(headers: Any, key: str) -> str:
    """按响应头名称读取字符串值。"""
    if hasattr(headers, "get"):
        value = headers.get(key)
        return str(value).strip() if value is not None else ""
    return ""


def codex_limit_mapping(primary_minutes: int | None, secondary_minutes: int | None) -> tuple[bool, bool]:
    """判断 primary/secondary 分别对应 5小时还是7天窗口。"""
    if primary_minutes is not None and secondary_minutes is not None:
        return primary_minutes < secondary_minutes, primary_minutes >= secondary_minutes
    if primary_minutes is not None:
        return primary_minutes <= 360, primary_minutes > 360
    if secondary_minutes is not None:
        return secondary_minutes > 360, secondary_minutes <= 360
    return False, True


def build_limit_record(source: dict[str, Any], updated_at: str) -> dict[str, Any]:
    """生成单个限额窗口的展示记录。"""
    record = compact_none({
        "used_percent": source.get("used_percent"),
        "reset_after_seconds": source.get("reset_after_seconds"),
        "window_minutes": source.get("window_minutes"),
    })
    reset_after = source.get("reset_after_seconds")
    if isinstance(reset_after, int):
        base = parse_time_value(updated_at) or datetime.now(timezone.utc)
        record["reset_at"] = format_datetime(base + timedelta(seconds=max(0, reset_after)))
    return record


def public_codex_usage(usage: Any) -> dict[str, Any]:
    """生成会随时间递减的限额展示数据。"""
    if not isinstance(usage, dict):
        return {}
    result = dict(usage)
    for key in ("limit_5h", "limit_7d"):
        result[key] = public_limit_record(usage.get(key))
    return result


def public_limit_record(limit: Any) -> dict[str, Any]:
    """按重置时间修正单个限额窗口。"""
    if not isinstance(limit, dict) or not limit:
        return {}
    record = dict(limit)
    reset_at = parse_time_value(record.get("reset_at"))
    if reset_at is None:
        return record
    remaining = int((reset_at - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        record["used_percent"] = 0
        record["reset_after_seconds"] = 0
    else:
        record["reset_after_seconds"] = remaining
    return record


def compact_none(values: dict[str, Any]) -> dict[str, Any]:
    """移除空值字段。"""
    return {key: value for key, value in values.items() if value is not None}


def build_config_examples(base_url: str) -> dict[str, str]:
    """生成页面展示的 Codex CLI 配置。"""
    codex_base = base_url.rstrip("/") + "/backend-api/codex"
    return {
        "recommended_toml": (
            f'model = "{CC_SWITCH_CODEX_MODEL}"\n\n'
            "[model_providers.openai]\n"
            'name = "local-codex-relay"\n'
            f'base_url = "{codex_base}"\n'
            'wire_api = "responses"\n'
        ),
        "auth_json": json.dumps({"OPENAI_API_KEY": LOCAL_API_KEY}, ensure_ascii=False, indent=2),
    }


def build_ccswitch_import_deeplink(base_url: str) -> str:
    """生成 CC-Switch 导入链接。"""
    clean_base = base_url.rstrip("/")
    endpoint = clean_base + "/backend-api/codex"
    usage_script = f"""({{
    request: {{
      url: "{clean_base}/api/status",
      method: "GET"
    }},
    extractor: function(response) {{
      const usage = response?.codex_usage || {{}};
      const fiveHour = usage?.limit_5h || {{}};
      const sevenDay = usage?.limit_7d || {{}};
      const isValid = !!response?.has_session && !response?.session?.expired;
      function buildWindow(name, limit) {{
        const used = typeof limit.used_percent === "number" ? limit.used_percent : 0;
        return {{
          isValid: isValid,
          planName: name,
          total: 100,
          used: used,
          remaining: Math.max(0, 100 - used),
          unit: "%",
          extra: limit.reset_at ? "重置 " + limit.reset_at : ""
        }};
      }}
      return [
        buildWindow("5h", fiveHour),
        buildWindow("7d", sevenDay)
      ];
    }}
  }})"""
    entries = [
        ("resource", "provider"),
        ("app", "codex"),
        ("model", CC_SWITCH_CODEX_MODEL),
        ("name", "Local Codex Relay"),
        ("homepage", clean_base),
        ("endpoint", endpoint),
        ("apiKey", LOCAL_API_KEY),
        ("configFormat", "json"),
        ("usageEnabled", "true"),
        ("usageScript", base64.b64encode(usage_script.encode("utf-8")).decode("ascii")),
        ("usageAutoInterval", "30"),
    ]
    return "ccswitch://v1/import?" + urllib.parse.urlencode(entries)


def open_ccswitch_import(base_url: str) -> None:
    """调用系统协议处理器导入 CC-Switch。"""
    deeplink = build_ccswitch_import_deeplink(base_url)
    if hasattr(os, "startfile"):
        os.startfile(deeplink)  # type: ignore[attr-defined]
        return
    raise RuntimeError("当前系统不支持直接打开 CC-Switch 协议")


class CodexRelayHandler(BaseHTTPRequestHandler):
    """处理状态 API 和 Codex 代理请求。"""

    server_version = "CodexSessionRelay/0.1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """处理 GET 请求。"""
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.write_error_json(HTTPStatus.NOT_FOUND, "desktop_app_required", "请使用桌面窗口导入和查看状态")
            return
        if path == "/api/status":
            self.write_json(self.state.status(self.base_url()))
            return
        if path == "/api/refresh-usage":
            self.handle_refresh_usage()
            return
        self.write_error_json(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")

    def do_POST(self) -> None:
        """处理 POST 请求。"""
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/import-session":
            self.handle_import_session()
            return
        if path == "/api/switch-session":
            self.handle_switch_session()
            return
        if path == "/api/clear-session":
            self.state.clear_session()
            self.write_json({"ok": True})
            return
        if is_proxy_path(path):
            self.handle_proxy(path)
            return
        self.write_error_json(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")

    @property
    def state(self) -> RelayState:
        """返回共享状态。"""
        return self.server.state  # type: ignore[attr-defined]

    def handle_import_session(self) -> None:
        """导入 api/auth/session。"""
        try:
            body = self.read_body()
            payload = json.loads(body.decode("utf-8"))
            session = self.state.import_session(payload)
            self.write_json({"ok": True, "session": session})
        except Exception as exc:
            self.write_error_json(HTTPStatus.BAD_REQUEST, "import_failed", str(exc))

    def handle_switch_session(self) -> None:
        """切换当前激活账号。"""
        try:
            body = self.read_body()
            payload = json.loads(body.decode("utf-8"))
            key = string_value(payload.get("key")) if isinstance(payload, dict) else ""
            if not key or not self.state.set_active_session(key):
                self.write_error_json(HTTPStatus.BAD_REQUEST, "switch_failed", "账号不存在")
                return
            self.write_json({"ok": True})
        except Exception as exc:
            self.write_error_json(HTTPStatus.BAD_REQUEST, "switch_failed", str(exc))

    def handle_refresh_usage(self) -> None:
        """主动刷新当前账号的 Codex 限额。"""
        try:
            usage = self.state.refresh_codex_usage()
            self.write_json({"ok": True, "codex_usage": usage, "status": self.state.status(self.base_url())})
        except ValueError as exc:
            self.write_error_json(HTTPStatus.BAD_REQUEST, "refresh_failed", str(exc))
        except Exception as exc:
            self.write_error_json(HTTPStatus.BAD_GATEWAY, "refresh_failed", sanitize_error(str(exc)))

    def handle_proxy(self, path: str) -> None:
        """把 Codex 请求代理到 ChatGPT 上游。"""
        started_at = time.time()
        log_entry = {
            "time": format_datetime(datetime.now(timezone.utc)),
            "path": path,
            "status": 0,
            "duration_ms": 0,
            "error": "",
        }
        session = self.state.get_session()
        if not session:
            log_entry["status"] = 401
            log_entry["error"] = "未导入 session"
            log_entry["duration_ms"] = elapsed_ms(started_at)
            self.state.add_log(log_entry)
            self.write_error_json(HTTPStatus.UNAUTHORIZED, "session_required", "请先导入 api/auth/session")
            return

        public = public_session(session)
        if public and public.get("expired"):
            log_entry["status"] = 401
            log_entry["error"] = "accessToken 已过期"
            log_entry["duration_ms"] = elapsed_ms(started_at)
            self.state.add_log(log_entry)
            self.write_error_json(HTTPStatus.UNAUTHORIZED, "session_expired", "accessToken 已过期，请重新导入 api/auth/session")
            return

        try:
            body = self.read_body()
            upstream_url = build_upstream_url(path)
            req = urllib.request.Request(
                upstream_url,
                data=body,
                method="POST",
                headers=self.build_upstream_headers(session),
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                log_entry["status"] = int(resp.status)
                self.state.update_codex_usage(resp.headers)
                self.send_response(resp.status, resp.reason)
                self.copy_response_headers(resp.headers)
                self.end_headers()
                while True:
                    chunk = resp.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as exc:
            log_entry["status"] = int(exc.code)
            self.state.update_codex_usage(exc.headers)
            body = exc.read()
            self.send_response(exc.code, exc.reason)
            self.copy_response_headers(exc.headers)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            log_entry["status"] = 502
            log_entry["error"] = sanitize_error(str(exc))
            self.write_error_json(HTTPStatus.BAD_GATEWAY, "upstream_error", f"上游请求失败: {log_entry['error']}")
            print(f"[relay] 上游连接失败: {log_entry['error']}", file=sys.stderr)
        except Exception as exc:
            log_entry["status"] = 502
            log_entry["error"] = sanitize_error(str(exc))
            self.write_error_json(HTTPStatus.BAD_GATEWAY, "upstream_error", f"上游请求失败: {log_entry['error']}")
            traceback.print_exc()
        finally:
            log_entry["duration_ms"] = elapsed_ms(started_at)
            self.state.add_log(log_entry)

    def build_upstream_headers(self, session: dict[str, Any]) -> dict[str, str]:
        """构建上游请求头。"""
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in PASS_REQUEST_HEADERS and value.strip():
                headers[key] = value
        headers["Authorization"] = "Bearer " + session["access_token"]
        account_id = string_value(session.get("account_id"))
        if account_id:
            headers["chatgpt-account-id"] = account_id
        headers.setdefault("OpenAI-Beta", "responses=experimental")
        headers.setdefault("originator", "codex_cli_rs")
        headers.setdefault("Accept", "text/event-stream")
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", "codex_cli_rs/0.125.0")
        return headers

    def copy_response_headers(self, headers: Any) -> None:
        """复制安全响应头。"""
        for key, value in headers.items():
            if key.lower() in DROP_RESPONSE_HEADERS:
                continue
            self.send_header(key, value)
        self.send_header("Connection", "close")

    def read_body(self) -> bytes:
        """读取请求体并限制大小。"""
        length = int(self.headers.get("Content-Length") or "0")
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        return self.rfile.read(length) if length > 0 else b""

    def base_url(self) -> str:
        """生成当前服务 base URL。"""
        host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"http://{host}"

    def write_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        """输出 JSON 响应。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def write_error_json(self, status: int, code: str, message: str) -> None:
        """输出统一错误响应。"""
        self.write_json({"ok": False, "error": {"code": code, "message": message}}, status)

    def log_message(self, fmt: str, *args: Any) -> None:
        """输出不含敏感信息的访问日志。"""
        print(f"[relay] {self.address_string()} {fmt % args}")


def is_proxy_path(path: str) -> bool:
    """判断是否是 Codex 代理路径。"""
    return path == "/backend-api/codex/responses" or path.startswith("/backend-api/codex/responses/") or path == "/v1/responses" or path.startswith("/v1/responses/")


def build_upstream_url(path: str) -> str:
    """根据本地路径生成上游 Codex URL。"""
    suffix = ""
    for prefix in ("/backend-api/codex/responses", "/v1/responses"):
        if path == prefix:
            suffix = ""
            break
        if path.startswith(prefix + "/"):
            suffix = path[len(prefix) :]
            break
    return UPSTREAM_CODEX_RESPONSES + posixpath.normpath("/" + suffix.lstrip("/")).rstrip("/") if suffix else UPSTREAM_CODEX_RESPONSES


def elapsed_ms(started_at: float) -> int:
    """计算耗时毫秒。"""
    return max(0, int((time.time() - started_at) * 1000))


def sanitize_error(message: str) -> str:
    """限制错误摘要长度。"""
    message = " ".join(message.split())
    if len(message) > 180:
        return message[:180] + "..."
    return message


def load_store_file(store_path: str) -> dict[str, Any]:
    """读取统一存储文件。"""
    if not os.path.exists(store_path):
        return {}
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[relay] 存储文件加载失败: {exc}", file=sys.stderr)
        return {}


def save_store_file(store_path: str, data: dict[str, Any]) -> None:
    """写入统一存储文件。"""
    tmp_path = store_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, store_path)


def load_settings(store_path: str) -> dict[str, Any]:
    """加载本地设置。"""
    data = {"port": DEFAULT_PORT, "startup": is_startup_enabled(), "close_action": ""}
    saved = load_store_file(store_path)
    if not saved:
        return data
    try:
        port = int(saved.get("port") or DEFAULT_PORT)
        if 1 <= port <= 65535:
            data["port"] = port
    except (TypeError, ValueError):
        pass
    data["startup"] = is_startup_enabled()
    close_action = string_value(saved.get("close_action"))
    if close_action in (CLOSE_ACTION_EXIT, CLOSE_ACTION_MINIMIZE):
        data["close_action"] = close_action
    return data


def save_settings(store_path: str, settings: dict[str, Any]) -> None:
    """保存本地设置。"""
    data = load_store_file(store_path)
    data.update(settings)
    data["version"] = 3
    save_store_file(store_path, data)


def startup_command() -> str:
    """生成开机启动命令。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = os.path.abspath(__file__)
    return f'"{sys.executable}" "{script}"'


def runtime_data_dir() -> str:
    """获取运行时数据文件目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def migrate_legacy_store(data_dir: str, store_path: str) -> None:
    """把旧的账号和设置文件合并到统一存储。"""
    if os.path.exists(store_path):
        return
    data: dict[str, Any] = {"version": 3}
    legacy_session_path = os.path.join(data_dir, LEGACY_SESSION_FILE)
    legacy_settings_path = os.path.join(data_dir, LEGACY_SETTINGS_FILE)
    session_data = load_store_file(legacy_session_path)
    if isinstance(session_data.get("sessions"), list):
        data["active_session_key"] = string_value(session_data.get("active_session_key"))
        data["sessions"] = [session_for_store(normalize_stored_session(item)) for item in session_data["sessions"] if isinstance(item, dict) and item.get("access_token")]
    elif session_data.get("access_token"):
        session = normalize_stored_session(session_data)
        data["active_session_key"] = session_identity_key(session)
        data["sessions"] = [session_for_store(session)]
    settings_data = load_store_file(legacy_settings_path)
    if settings_data:
        data.update({key: settings_data[key] for key in ("port", "close_action") if key in settings_data})
    if data.get("sessions") or settings_data:
        save_store_file(store_path, data)


def is_startup_enabled() -> bool:
    """检查当前用户开机启动是否启用。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_RUN_NAME)
        return string_value(value) == startup_command()
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> None:
    """设置当前用户开机启动。"""
    if winreg is None:
        raise RuntimeError("当前系统不支持 Windows 开机启动设置")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_RUN_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_RUN_NAME)
            except FileNotFoundError:
                pass


def choose_ui_font() -> tuple[str, int]:
    """选择更清晰的中文界面字体。"""
    families = set(tkfont.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "DengXian", "微软雅黑", "等线", "SimHei"):
        if name in families:
            return (name, 10)
    return ("Segoe UI", 10)


def choose_mono_font() -> tuple[str, int]:
    """选择配置文本使用的等宽字体。"""
    families = set(tkfont.families())
    for name in ("Cascadia Mono", "Consolas", "Courier New"):
        if name in families:
            return (name, 10)
    return ("Courier New", 10)


def find_existing_file(base_dir: str, names: tuple[str, ...]) -> str:
    """按候选文件名查找项目内图标文件。"""
    for name in names:
        path = os.path.join(base_dir, name)
        if os.path.exists(path):
            return path
    return ""


class WindowsTrayIcon:
    """使用 Windows 通知区托盘图标控制窗口显示。"""

    WM_TRAY = 0x0400 + 20
    ID_RESTORE = 1001
    ID_EXIT = 1002
    MF_STRING = 0x0000
    TPM_RIGHTBUTTON = 0x0002

    def __init__(self, app: "RelayDesktopApp") -> None:
        self.app = app
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.hwnd = 0
        self.visible = False
        self.nid: Any = None
        self.icon = 0
        self.wndproc: Any = None
        self.user32: Any = None
        self.shell32: Any = None

    def show(self) -> None:
        """显示托盘图标。"""
        if self.visible:
            return
        if self.thread is None:
            self.thread = threading.Thread(target=self.run_message_window, daemon=True)
            self.thread.start()
        if not self.ready.wait(2) or not self.hwnd:
            raise RuntimeError("托盘窗口初始化失败")
        if not self.shell32.Shell_NotifyIconW(0, ctypes.byref(self.nid)):
            raise RuntimeError("托盘图标添加失败")
        self.visible = True

    def hide(self) -> None:
        """移除托盘图标。"""
        if self.visible and self.shell32 and self.nid:
            self.shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))
            self.visible = False

    def close(self) -> None:
        """关闭托盘图标和消息窗口。"""
        self.hide()
        if self.user32 and self.hwnd:
            self.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)
        if self.user32 and self.icon:
            self.user32.DestroyIcon(self.icon)
            self.icon = 0

    def run_message_window(self) -> None:
        """创建隐藏消息窗口并接收托盘事件。"""
        from ctypes import wintypes

        self.user32 = ctypes.windll.user32
        self.shell32 = ctypes.windll.shell32
        hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HCURSOR),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", ctypes.c_wchar * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        def wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == self.WM_TRAY:
                if lparam in (0x0202, 0x0203):
                    self.app.root.after(0, self.app.show_from_tray)
                elif lparam == 0x0205:
                    self.show_menu(hwnd)
                return 0
            if msg == 0x0111:
                command = wparam & 0xFFFF
                if command == self.ID_RESTORE:
                    self.app.root.after(0, self.app.show_from_tray)
                elif command == self.ID_EXIT:
                    self.app.root.after(0, self.app.exit_app)
                return 0
            if msg == 0x0002:
                self.user32.PostQuitMessage(0)
                return 0
            return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        class_name = "CodexSessionRelayTrayWindow"
        self.wndproc = WNDPROC(wndproc)
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.DefWindowProcW.restype = LRESULT
        self.user32.CreateIcon.argtypes = [
            wintypes.HINSTANCE,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        self.user32.CreateIcon.restype = wintypes.HICON
        self.user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.LoadImageW.restype = wintypes.HICON
        self.user32.DestroyIcon.argtypes = [wintypes.HICON]
        self.user32.CreatePopupMenu.restype = wintypes.HMENU
        self.user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        self.user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.LPVOID,
        ]
        self.user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        wc = WNDCLASSW()
        wc.lpfnWndProc = self.wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        self.user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = self.user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, None, None, hinstance, None)
        self.icon = self.create_icon(hinstance)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = 1 | 2 | 4
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self.icon
        nid.szTip = "Codex Session Relay"
        self.nid = nid
        self.ready.set()

        msg = wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

    def show_menu(self, hwnd: int) -> None:
        """显示托盘右键菜单。"""
        import ctypes
        from ctypes import wintypes

        menu = self.user32.CreatePopupMenu()
        self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_RESTORE, "显示窗口")
        self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_EXIT, "退出程序")
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        self.user32.SetForegroundWindow(hwnd)
        self.user32.TrackPopupMenu(menu, self.TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
        self.user32.DestroyMenu(menu)

    def create_icon(self, hinstance: int) -> int:
        """创建 Relay 自定义托盘图标。"""
        if self.app.icon_ico_path:
            icon = self.user32.LoadImageW(None, self.app.icon_ico_path, 1, 0, 0, 0x00000010 | 0x00000040)
            if icon:
                return icon
        size = 32
        pixels = bytearray()
        for y in range(size - 1, -1, -1):
            for x in range(size):
                if x in (0, size - 1) or y in (0, size - 1):
                    color = (17, 24, 39)
                elif 7 <= x <= 24 and 7 <= y <= 24:
                    color = (15, 108, 189)
                elif 11 <= x <= 20 and 11 <= y <= 20:
                    color = (255, 255, 255)
                else:
                    color = (243, 244, 246)
                if 19 <= x <= 26 and 19 <= y <= 26:
                    color = (16, 185, 129)
                r, g, b = color
                pixels.extend((b, g, r, 255))
        xor_bits = (ctypes.c_ubyte * len(pixels)).from_buffer_copy(bytes(pixels))
        and_bits = (ctypes.c_ubyte * (size * size // 8))()
        return self.user32.CreateIcon(hinstance, size, size, 1, 32, and_bits, xor_bits)


class RelayDesktopApp:
    """Tkinter 桌面展示和后台代理控制。"""

    def __init__(self, state: RelayState, server: "RelayHTTPServer", host: str, port: int, store_path: str, settings: dict[str, Any]) -> None:
        self.state = state
        self.server = server
        self.host = host
        self.port = port
        self.store_path = store_path
        self.settings = dict(settings)
        self.base_url = f"http://{host}:{port}"
        self.app_dir = os.path.dirname(os.path.abspath(__file__))
        self.icon_ico_path = find_existing_file(self.app_dir, ICON_ICO_FILES)
        self.icon_image_path = find_existing_file(self.app_dir, ICON_IMAGE_FILES)
        self.window_icon_image: tk.PhotoImage | None = None
        self.root = tk.Tk()
        self.root.title("Codex Session Relay")
        self.root.geometry("980x650")
        self.root.minsize(860, 560)
        self.root.configure(bg="#f3f4f6")
        self.set_window_icon(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.status_vars: dict[str, tk.StringVar] = {}
        self.account_select_var = tk.StringVar(value="")
        self.account_options: dict[str, str] = {}
        self.base_url_var = tk.StringVar(value=f"本地代理 {self.base_url}")
        self.refresh_after_id: str | None = None
        self.tray_icon = WindowsTrayIcon(self) if sys.platform.startswith("win") else None
        self.build_ui()

    def set_window_icon(self, window: tk.Tk | tk.Toplevel) -> None:
        """设置窗口左上角和任务栏图标。"""
        try:
            if self.icon_ico_path and sys.platform.startswith("win"):
                window.iconbitmap(default=self.icon_ico_path)
            elif self.icon_image_path:
                if self.window_icon_image is None:
                    self.window_icon_image = tk.PhotoImage(file=self.icon_image_path)
                window.iconphoto(True, self.window_icon_image)
        except tk.TclError as exc:
            print(f"[relay] 窗口图标加载失败: {exc}", file=sys.stderr)

    def run(self) -> None:
        """启动后台代理并进入桌面窗口。"""
        self.server_thread.start()
        self.show_tray_icon()
        self.refresh(schedule=True)
        self.refresh_usage_on_start()
        self.root.mainloop()

    def restart_server(self, port: int) -> None:
        """按新端口重启本地 HTTP 代理。"""
        if port == self.port:
            return
        old_server = self.server
        new_server = RelayHTTPServer((self.host, port), CodexRelayHandler, self.state)
        old_server.shutdown()
        old_server.server_close()
        self.server = new_server
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.port = port
        self.base_url = f"http://{self.host}:{port}"
        self.base_url_var.set(f"本地代理 {self.base_url}")
        self.refresh(schedule=False)

    def build_ui(self) -> None:
        """构建桌面界面。"""
        style = ttk.Style()
        ui_font = choose_ui_font()
        mono_font = choose_mono_font()
        if sys.platform.startswith("win"):
            try:
                style.theme_use("vista")
            except tk.TclError:
                pass
        self.root.option_add("*Font", ui_font)
        style.configure(".", font=ui_font)
        style.configure("Title.TLabel", font=(ui_font[0], 16, "bold"))
        style.configure("Subtitle.TLabel", font=(ui_font[0], 9), foreground="#5f6b7a")
        style.configure("Section.TLabel", font=(ui_font[0], 10, "bold"))
        style.configure("Status.TLabel", font=(ui_font[0], 10, "bold"), foreground="#0f6cbd")
        style.configure("Primary.TButton", font=(ui_font[0], 10, "bold"), padding=(9, 4))
        style.configure("Tool.TButton", padding=(7, 3))

        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=5)
        outer.columnconfigure(1, weight=3)
        outer.rowconfigure(1, weight=0)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Codex Session Relay", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.base_url_var, style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.headline_var = tk.StringVar(value="未导入 session")
        ttk.Label(header, textvariable=self.headline_var, style="Status.TLabel").grid(row=0, column=1, sticky="e")
        command_bar = ttk.Frame(header)
        command_bar.grid(row=1, column=1, sticky="e")
        ttk.Button(command_bar, text="设置", style="Tool.TButton", command=self.open_settings).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(command_bar, text="获取 auth session", style="Tool.TButton", command=self.open_auth_session).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(command_bar, text="导入 CC-Switch", style="Primary.TButton", command=self.import_to_ccswitch).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(command_bar, text="刷新限额", style="Tool.TButton", command=self.refresh_usage).grid(row=0, column=3)

        import_box = self.create_card(outer)
        import_box.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(0, 8))
        import_box.rowconfigure(1, weight=1)
        import_box.columnconfigure(0, weight=1)
        ttk.Label(import_box, text="粘贴完整 JSON，相同覆盖, 不同新增。", style="Subtitle.TLabel").grid(row=0, column=1, sticky="e")
        self.session_text = scrolledtext.ScrolledText(import_box, height=8, wrap=tk.WORD, font=mono_font, relief=tk.FLAT, borderwidth=4)
        self.session_text.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(6, 5))
        ttk.Label(import_box, text="请粘贴完整Session JSON", style="Subtitle.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 5))
        import_actions = ttk.Frame(import_box)
        import_actions.grid(row=3, column=0, columnspan=2, sticky="ew")
        import_actions.columnconfigure(2, weight=1)
        ttk.Button(import_actions, text="导入", style="Primary.TButton", command=self.import_session).grid(row=0, column=0, sticky="w")
        ttk.Button(import_actions, text="编辑账号", style="Tool.TButton", command=self.open_account_editor).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.message_var = tk.StringVar(value="")
        ttk.Label(import_actions, textvariable=self.message_var, style="Subtitle.TLabel").grid(row=0, column=2, sticky="e")

        status_box = self.create_card(outer)
        status_box.grid(row=1, column=1, sticky="nsew")
        status_box.columnconfigure(1, weight=1)
        ttk.Label(status_box, text="当前账号", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        ttk.Label(status_box, text="切换账号").grid(row=1, column=0, sticky="w", pady=1)
        self.account_combo = ttk.Combobox(status_box, textvariable=self.account_select_var, state="readonly")
        self.account_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=1)
        self.account_combo.bind("<<ComboboxSelected>>", self.on_account_selected)
        for row, (key, label) in enumerate([
            ("email", "邮箱"),
            ("plan_type", "套餐类型"),
            ("expires", "过期时间"),
            ("limit_5h", "5小时限额"),
            ("limit_7d", "7天限额"),
        ], start=2):
            ttk.Label(status_box, text=label).grid(row=row, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="-")
            self.status_vars[key] = var
            ttk.Label(status_box, textvariable=var, wraplength=330).grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=1)
        self.ccswitch_var = tk.StringVar(value="")
        ttk.Separator(status_box).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 5))
        ttk.Label(status_box, textvariable=self.ccswitch_var, style="Subtitle.TLabel").grid(row=9, column=0, columnspan=2, sticky="w")

        config_box = self.create_card(outer)
        config_box.grid(row=2, column=1, sticky="nsew", pady=(8, 0))
        config_box.rowconfigure(2, weight=1)
        config_box.columnconfigure(0, weight=1)
        ttk.Label(config_box, text="Codex CLI 配置", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(config_box, text="手动配置时使用；CC-Switch 可直接点击顶部按钮导入。", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(1, 5))
        self.config_tabs = ttk.Notebook(config_box)
        self.config_tabs.grid(row=2, column=0, sticky="nsew")
        self.config_texts: dict[str, scrolledtext.ScrolledText] = {}
        for key, title in [("recommended_toml", "config.toml"), ("auth_json", "auth.json")]:
            frame = ttk.Frame(self.config_tabs, padding=3)
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            text = scrolledtext.ScrolledText(frame, height=7, wrap=tk.NONE, font=mono_font, relief=tk.FLAT, borderwidth=4)
            text.grid(row=0, column=0, sticky="nsew")
            self.config_texts[key] = text
            self.config_tabs.add(frame, text=title)

    def create_card(self, parent: ttk.Frame) -> ttk.Frame:
        """创建统一留白的内容面板。"""
        frame = ttk.Frame(parent, padding=6)
        return frame

    def import_session(self) -> None:
        """从文本框导入 session。"""
        content = self.session_text.get("1.0", tk.END).strip()
        if not content:
            self.message_var.set("请先粘贴 session JSON")
            return
        try:
            self.state.import_session({"content": content})
        except Exception as exc:
            self.message_var.set(f"导入失败: {exc}")
            messagebox.showerror("导入失败", str(exc))
            return
        self.session_text.delete("1.0", tk.END)
        self.message_var.set("导入成功")
        self.refresh(schedule=False)

    def open_auth_session(self) -> None:
        """打开 ChatGPT auth session 页面。"""
        webbrowser.open("https://chatgpt.com/api/auth/session")

    def open_settings(self) -> None:
        """打开设置窗口。"""
        self.root.deiconify()
        self.root.lift()
        SettingsWindow(self)

    def import_to_ccswitch(self) -> None:
        """把本地 Codex Relay 配置导入 CC-Switch。"""
        try:
            open_ccswitch_import(self.base_url)
        except Exception as exc:
            self.ccswitch_var.set("导入失败")
            messagebox.showerror("导入失败", f"无法打开 CC-Switch：{exc}")
            return
        self.ccswitch_var.set("已调用 CC-Switch 导入")

    def on_account_selected(self, _event: Any) -> None:
        """切换下拉框选中的账号。"""
        key = self.account_options.get(self.account_select_var.get(), "")
        if key and self.state.set_active_session(key):
            self.message_var.set("已切换账号")
            self.refresh(schedule=False)

    def open_account_editor(self) -> None:
        """打开账号编辑窗口。"""
        AccountEditorWindow(self)

    def refresh_usage_on_start(self) -> None:
        """启动后自动刷新一次 Codex 限额。"""
        if self.state.get_session():
            threading.Thread(target=self.refresh_usage_worker, args=(True,), daemon=True).start()

    def refresh_usage(self) -> None:
        """主动请求上游刷新 Codex 限额。"""
        threading.Thread(target=self.refresh_usage_worker, daemon=True).start()

    def refresh_usage_worker(self, silent: bool = False) -> None:
        """后台执行限额刷新，避免卡住界面。"""
        try:
            self.state.refresh_codex_usage()
        except Exception as exc:
            self.root.after(0, self.refresh_usage_failed, str(exc), silent)
            return
        self.root.after(0, self.refresh_usage_done)

    def refresh_usage_done(self) -> None:
        """刷新限额成功后更新界面。"""
        self.refresh(schedule=False)

    def refresh_usage_failed(self, message: str, silent: bool = False) -> None:
        """刷新限额失败后提示用户。"""
        self.message_var.set(f"刷新失败: {message}")
        if not silent:
            messagebox.showerror("刷新失败", message)
        self.refresh(schedule=False)

    def refresh(self, schedule: bool = False) -> None:
        """刷新账号状态、配置和日志。"""
        if self.refresh_after_id is not None:
            self.root.after_cancel(self.refresh_after_id)
            self.refresh_after_id = None
        data = self.state.status(self.base_url)
        session = data.get("session")
        self.refresh_account_options(data)
        codex_usage = data.get("codex_usage", {})
        if session:
            self.headline_var.set("session 已过期" if session.get("expired") else "session 可用")
            values = {
                "email": session.get("email") or "-",
                "plan_type": session.get("plan_type") or "-",
                "expires": session.get("expires") or "-",
                "limit_5h": format_limit(codex_usage.get("limit_5h")),
                "limit_7d": format_limit(codex_usage.get("limit_7d")),
            }
        else:
            self.headline_var.set("未导入 session")
            values = {key: "-" for key in self.status_vars}
        for key, value in values.items():
            self.status_vars[key].set(str(value))

        config = data.get("config", {})
        for key, text in self.config_texts.items():
            text.delete("1.0", tk.END)
            text.insert("1.0", config.get(key, ""))

        if schedule:
            self.refresh_after_id = self.root.after(3000, self.refresh, True)

    def refresh_account_options(self, data: dict[str, Any]) -> None:
        """刷新账号切换下拉框。"""
        sessions = data.get("sessions", [])
        self.account_options = {}
        names: list[str] = []
        for item in sessions:
            if not isinstance(item, dict):
                continue
            key = string_value(item.get("key"))
            if not key:
                continue
            name = string_value(item.get("display_name")) or key
            base_name = name
            index = 2
            while name in self.account_options:
                name = f"{base_name} #{index}"
                index += 1
            self.account_options[name] = key
            names.append(name)
        self.account_combo["values"] = names
        active_key = string_value(data.get("active_session_key"))
        for name, key in self.account_options.items():
            if key == active_key:
                self.account_select_var.set(name)
                break
        else:
            self.account_select_var.set("")

    def close(self) -> None:
        """按用户设置处理主窗口关闭。"""
        close_action = string_value(self.settings.get("close_action"))
        if close_action not in (CLOSE_ACTION_EXIT, CLOSE_ACTION_MINIMIZE):
            dialog = CloseChoiceDialog(self)
            close_action = dialog.result
            if not close_action:
                return
            self.settings["close_action"] = close_action
            save_settings(self.store_path, self.settings)
        if close_action == CLOSE_ACTION_MINIMIZE:
            self.hide_to_tray()
            return
        self.exit_app()

    def hide_to_tray(self) -> None:
        """隐藏主窗口到系统托盘。"""
        if self.show_tray_icon():
            self.root.withdraw()
            return
        self.root.iconify()

    def show_tray_icon(self) -> bool:
        """确保系统托盘图标可见。"""
        if self.tray_icon is None:
            return False
        try:
            self.tray_icon.show()
            return True
        except Exception as exc:
            print(f"[relay] 托盘图标初始化失败: {exc}", file=sys.stderr)
            return False

    def show_from_tray(self) -> None:
        """从系统托盘恢复主窗口。"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def exit_app(self) -> None:
        """退出窗口和后台代理服务。"""
        if self.refresh_after_id is not None:
            self.root.after_cancel(self.refresh_after_id)
            self.refresh_after_id = None
        if self.tray_icon is not None:
            self.tray_icon.close()
            self.tray_icon = None
        self.server.shutdown()
        self.server.server_close()
        self.root.destroy()


class AccountEditorWindow:
    """账号编辑弹窗。"""

    def __init__(self, app: RelayDesktopApp) -> None:
        self.app = app
        self.state = app.state
        self.selected_key = ""
        self.accounts: dict[str, str] = {}
        self.window = tk.Toplevel(app.root)
        app.set_window_icon(self.window)
        self.window.title("编辑账号")
        self.window.geometry("1080x560")
        self.window.minsize(980, 500)
        self.window.transient(app.root)
        self.window.grab_set()
        self.message_var = tk.StringVar(value="")
        self.build_ui()
        self.refresh_accounts()
        self.center_over_parent()

    def build_ui(self) -> None:
        """构建账号编辑窗口。"""
        outer = ttk.Frame(self.window, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=0, minsize=300)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(outer, text="账号列表", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(outer, text="编辑选中账号的 Session JSON", style="Section.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0), pady=(0, 8))

        list_box = ttk.Frame(outer)
        list_box.grid(row=1, column=0, sticky="nsew")
        list_box.rowconfigure(0, weight=1)
        list_box.columnconfigure(0, weight=1)
        self.account_list = tk.Listbox(list_box, exportselection=False, height=12, width=40, xscrollcommand=lambda *args: self.account_xscroll.set(*args))
        self.account_list.grid(row=0, column=0, sticky="nsew")
        self.account_xscroll = ttk.Scrollbar(list_box, orient=tk.HORIZONTAL, command=self.account_list.xview)
        self.account_xscroll.grid(row=1, column=0, sticky="ew")
        self.account_list.bind("<<ListboxSelect>>", self.on_select)

        edit_box = ttk.Frame(outer)
        edit_box.grid(row=1, column=1, sticky="nsew", padx=(12, 0))
        edit_box.rowconfigure(0, weight=1)
        edit_box.columnconfigure(0, weight=1)
        self.session_text = scrolledtext.ScrolledText(edit_box, wrap=tk.WORD, font=choose_mono_font(), relief=tk.FLAT, borderwidth=8)
        self.session_text.grid(row=0, column=0, sticky="nsew")
        ttk.Label(edit_box, text="请粘贴同一个账号的新 Session JSON", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        actions.columnconfigure(2, weight=1)
        ttk.Button(actions, text="更新选中账号", style="Primary.TButton", command=self.update_selected).grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="删除选中账号", style="Tool.TButton", command=self.delete_selected).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(actions, textvariable=self.message_var, style="Subtitle.TLabel").grid(row=0, column=2, sticky="e")

    def center_over_parent(self) -> None:
        """把编辑窗口定位到主窗口上方居中。"""
        self.window.update_idletasks()
        parent_x = self.app.root.winfo_rootx()
        parent_y = self.app.root.winfo_rooty()
        parent_w = self.app.root.winfo_width()
        parent_h = self.app.root.winfo_height()
        win_w = self.window.winfo_width()
        win_h = self.window.winfo_height()
        x = parent_x + max(0, (parent_w - win_w) // 2)
        y = parent_y + max(0, (parent_h - win_h) // 3)
        self.window.geometry(f"+{x}+{y}")

    def refresh_accounts(self) -> None:
        """刷新账号列表。"""
        self.account_list.delete(0, tk.END)
        self.accounts = {}
        active_key = self.state.active_session_key
        for item in self.state.public_sessions():
            key = string_value(item.get("key"))
            if not key:
                continue
            name = string_value(item.get("display_name")) or key
            if key == active_key:
                name += "  当前"
            self.accounts[name] = key
            self.account_list.insert(tk.END, name)
        if self.account_list.size() > 0:
            self.account_list.selection_set(0)
            self.on_select(None)

    def on_select(self, _event: Any) -> None:
        """记录当前选中账号。"""
        selection = self.account_list.curselection()
        if not selection:
            self.selected_key = ""
            return
        name = self.account_list.get(selection[0])
        self.selected_key = self.accounts.get(name, "")
        self.session_text.delete("1.0", tk.END)
        session_json = self.state.export_session_json(self.selected_key)
        if session_json:
            self.session_text.insert("1.0", session_json)

    def update_selected(self) -> None:
        """用输入内容更新选中账号。"""
        if not self.selected_key:
            self.message_var.set("请先选择账号")
            return
        content = self.session_text.get("1.0", tk.END).strip()
        if not content:
            self.message_var.set("请先粘贴 Session JSON")
            return
        try:
            self.state.update_session(self.selected_key, {"content": content})
        except Exception as exc:
            self.message_var.set(f"更新失败: {exc}")
            messagebox.showerror("更新失败", str(exc), parent=self.window)
            return
        self.session_text.delete("1.0", tk.END)
        self.message_var.set("更新成功")
        self.refresh_accounts()
        self.app.refresh(schedule=False)

    def delete_selected(self) -> None:
        """删除选中账号。"""
        if not self.selected_key:
            self.message_var.set("请先选择账号")
            return
        if not messagebox.askyesno("确认删除", "确定要删除选中的账号吗？", parent=self.window):
            return
        if self.state.delete_session(self.selected_key):
            self.message_var.set("已删除")
            self.selected_key = ""
            self.session_text.delete("1.0", tk.END)
            self.refresh_accounts()
            self.app.refresh(schedule=False)
        else:
            self.message_var.set("账号不存在")


class CloseChoiceDialog:
    """首次关闭主窗口时选择退出方式。"""

    def __init__(self, app: RelayDesktopApp) -> None:
        self.app = app
        self.result = ""
        self.window = tk.Toplevel(app.root)
        app.set_window_icon(self.window)
        self.window.title("关闭方式")
        self.window.geometry("360x170")
        self.window.resizable(False, False)
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.build_ui()
        self.center_over_parent()
        self.window.wait_window()

    def build_ui(self) -> None:
        """构建关闭方式选择窗口。"""
        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(outer, text="关闭窗口时要执行什么操作？", style="Section.TLabel").pack(anchor="w")
        ttk.Label(outer, text="本次选择会保存，之后可在设置里修改。", style="Subtitle.TLabel").pack(anchor="w", pady=(6, 14))

        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="隐藏到托盘", style="Tool.TButton", command=lambda: self.choose(CLOSE_ACTION_MINIMIZE)).pack(side=tk.LEFT)
        ttk.Button(actions, text="退出程序", style="Primary.TButton", command=lambda: self.choose(CLOSE_ACTION_EXIT)).pack(side=tk.LEFT, padx=(10, 0))

    def center_over_parent(self) -> None:
        """把选择窗口定位到主窗口上方居中。"""
        self.window.update_idletasks()
        parent_x = self.app.root.winfo_rootx()
        parent_y = self.app.root.winfo_rooty()
        parent_w = self.app.root.winfo_width()
        parent_h = self.app.root.winfo_height()
        win_w = self.window.winfo_width()
        win_h = self.window.winfo_height()
        x = parent_x + max(0, (parent_w - win_w) // 2)
        y = parent_y + max(0, (parent_h - win_h) // 3)
        self.window.geometry(f"+{x}+{y}")

    def choose(self, action: str) -> None:
        """保存用户选择并关闭弹窗。"""
        self.result = action
        self.window.destroy()

    def cancel(self) -> None:
        """取消本次关闭操作。"""
        self.result = ""
        self.window.destroy()


class SettingsWindow:
    """设置弹窗。"""

    def __init__(self, app: RelayDesktopApp) -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        app.set_window_icon(self.window)
        self.window.title("设置")
        self.window.geometry("460x290")
        self.window.minsize(420, 260)
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.deiconify()
        self.window.lift(app.root)
        self.window.focus_force()
        self.port_var = tk.StringVar(value=str(app.port))
        self.startup_var = tk.BooleanVar(value=is_startup_enabled())
        close_action = string_value(app.settings.get("close_action")) or CLOSE_ACTION_EXIT
        self.exit_on_close_var = tk.BooleanVar(value=close_action == CLOSE_ACTION_EXIT)
        self.minimize_on_close_var = tk.BooleanVar(value=close_action == CLOSE_ACTION_MINIMIZE)
        self.message_var = tk.StringVar(value="")
        self.build_ui()
        self.center_over_parent()

    def build_ui(self) -> None:
        """构建设置窗口。"""
        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="监听端口").grid(row=0, column=0, sticky="w", pady=(0, 10))
        port_entry = ttk.Entry(outer, textvariable=self.port_var, width=12)
        port_entry.grid(row=0, column=1, sticky="w", pady=(0, 10))

        ttk.Label(outer, text="开机启动").grid(row=1, column=0, sticky="w", pady=(0, 10))
        ttk.Checkbutton(outer, text="随 Windows 启动", variable=self.startup_var).grid(row=1, column=1, sticky="w", pady=(0, 10))

        ttk.Label(outer, text="退出时").grid(row=2, column=0, sticky="nw", pady=(0, 10))
        close_box = ttk.Frame(outer)
        close_box.grid(row=2, column=1, sticky="w", pady=(0, 10))
        ttk.Checkbutton(close_box, text="直接退出程序", variable=self.exit_on_close_var, command=lambda: self.select_close_action(CLOSE_ACTION_EXIT)).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(close_box, text="隐藏到系统托盘", variable=self.minimize_on_close_var, command=lambda: self.select_close_action(CLOSE_ACTION_MINIMIZE)).grid(row=1, column=0, sticky="w", pady=(6, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        actions.columnconfigure(1, weight=1)
        ttk.Button(actions, text="保存设置", style="Primary.TButton", command=self.save).grid(row=0, column=0, sticky="w")
        ttk.Label(actions, textvariable=self.message_var, style="Subtitle.TLabel").grid(row=0, column=1, sticky="e")

    def center_over_parent(self) -> None:
        """把设置窗口定位到主窗口上方居中。"""
        self.window.update_idletasks()
        parent_x = self.app.root.winfo_rootx()
        parent_y = self.app.root.winfo_rooty()
        parent_w = self.app.root.winfo_width()
        parent_h = self.app.root.winfo_height()
        win_w = self.window.winfo_width()
        win_h = self.window.winfo_height()
        x = parent_x + max(0, (parent_w - win_w) // 2)
        y = parent_y + max(0, (parent_h - win_h) // 3)
        self.window.geometry(f"+{x}+{y}")

    def save(self) -> None:
        """保存设置并应用变更。"""
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("保存失败", "端口必须是数字", parent=self.window)
            return
        if port < 1 or port > 65535:
            messagebox.showerror("保存失败", "端口范围必须是 1-65535", parent=self.window)
            return
        startup = bool(self.startup_var.get())
        close_action = CLOSE_ACTION_MINIMIZE if self.minimize_on_close_var.get() else CLOSE_ACTION_EXIT
        try:
            self.app.restart_server(port)
            set_startup_enabled(startup)
            self.app.settings.update({"port": port, "startup": startup, "close_action": close_action})
            save_settings(self.app.store_path, self.app.settings)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.window)
            return
        self.message_var.set("已保存")

    def select_close_action(self, action: str) -> None:
        """保持退出方式两个复选框互斥。"""
        if action == CLOSE_ACTION_MINIMIZE:
            self.minimize_on_close_var.set(True)
            self.exit_on_close_var.set(False)
        else:
            self.exit_on_close_var.set(True)
            self.minimize_on_close_var.set(False)


def format_remaining(seconds: Any) -> str:
    """格式化剩余有效时间。"""
    if seconds is None:
        return "-"
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if value <= 0:
        return "已过期"
    days, rem = divmod(value, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m"


def format_limit(limit: Any) -> str:
    """格式化 Codex 限额窗口。"""
    if not isinstance(limit, dict) or not limit:
        return "等待首次请求返回限额"
    used = limit.get("used_percent")
    reset_after = limit.get("reset_after_seconds")
    parts = []
    if isinstance(used, (int, float)):
        parts.append(f"已用 {used:g}%")
    if isinstance(reset_after, int):
        parts.append(f"重置剩余 {format_remaining(reset_after)}")
    return "  ".join(parts) if parts else "等待首次请求返回限额"


class RelayHTTPServer(ThreadingHTTPServer):
    """带共享状态的 HTTPServer。"""

    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler], state: RelayState) -> None:
        super().__init__(server_address, handler_cls)
        self.state = state


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="本地 Codex Session Relay")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="监听端口，默认读取设置文件或使用 8765")
    parser.add_argument("--store", default="", help="存储文件路径，默认脚本同目录 relay_store.json")
    parser.add_argument("--settings", default="", help="兼容旧参数，当前已合并到 --store")
    parser.add_argument("--no-gui", action="store_true", help="不启动桌面窗口，仅运行代理服务")
    return parser.parse_args()


def main() -> int:
    """启动本地服务。"""
    args = parse_args()
    data_dir = runtime_data_dir()
    store_path = args.store or os.path.join(data_dir, STORE_FILE)
    if args.settings and not args.store:
        store_path = args.settings
    migrate_legacy_store(data_dir, store_path)
    settings = load_settings(store_path)
    port = args.port if args.port > 0 else int(settings.get("port") or DEFAULT_PORT)
    state = RelayState(store_path)
    server = RelayHTTPServer((args.host, port), CodexRelayHandler, state)
    base_url = f"http://{args.host}:{port}"
    print("[relay] Codex Session Relay 已启动")
    print(f"[relay] 桌面窗口: {'关闭' if args.no_gui else '开启'}")
    print(f"[relay] 推荐 base_url: {base_url}/backend-api/codex")
    print("[relay] 按 Ctrl+C 停止")
    if not args.no_gui:
        app = RelayDesktopApp(state, server, args.host, port, store_path, settings)
        app.run()
        return 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[relay] 正在停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
