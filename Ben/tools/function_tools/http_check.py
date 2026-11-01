"""http_check @function_tool（对齐 strix 的 @function_tool 工具模式）。

工具从 ctx.context 取 SDK sandbox session，通过 session.exec() 在容器内
执行 curl 命令。返回 JSON 字符串供模型阅读。

ctx.context 结构：
    {
        "sandbox_session": SandboxSession,  # SDK 的 sandbox 会话
        "allowed_targets": tuple[str, ...],
        "command_timeout": float,
        "target": str,
    }

注意：本工具是同步函数，SDK 在线程池中调用它。sandbox_session.exec() 是
异步方法，因此内部用 asyncio.run() 在新事件循环中执行。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit

from agents import function_tool
from agents.run_context import RunContextWrapper

# 标记行前缀：curl -w 输出的元信息，便于从 stdout 中切分
_META_PREFIX = "__BEN_META__"
_W_TEMPLATE = f"{_META_PREFIX}%{{http_code}}|%{{size_download}}|%{{time_total}}|%{{url_effective}}"

# 关键安全响应头（仅检查是否存在；缺失=加固建议，不是漏洞）
_SECURITY_HEADERS = (
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Content-Security-Policy",
    "Strict-Transport-Security",
    "Referrer-Policy",
    "Permissions-Policy",
)

# 合法 HTTP 头名（RFC 7230 token 字符集）
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_READONLY_METHODS = ("GET", "HEAD")


def _origin(url: str) -> str | None:
    """提取 URL 的 origin（scheme://host[:port]），非 http/https 返回 None。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.port is not None:
        return f"{parts.scheme}://{parts.hostname}:{parts.port}"
    return f"{parts.scheme}://{parts.hostname}"


def _allowed_origins(allowed_targets: tuple[str, ...]) -> set[str]:
    """把白名单配置归一化为 origin 集合。"""
    origins: set[str] = set()
    for target in allowed_targets:
        org = _origin(target)
        if org:
            origins.add(org)
    return origins


def _validate_custom_headers(headers: dict[str, str]) -> list[str]:
    """校验自定义头，返回 curl 的 -H 参数列表。"""
    h_args: list[str] = []
    for name, value in headers.items():
        if not _HEADER_NAME_RE.match(name):
            raise ValueError(f"非法请求头名：{name!r}")
        if any(ch in value for ch in "\r\n"):
            raise ValueError(f"请求头 {name} 的值禁止包含回车/换行（防头注入）")
        h_args.extend(["-H", f"{name}: {value}"])
    return h_args


def _build_curl(
    *,
    url: str,
    method: str,
    follow_redirects: bool,
    headers: dict[str, str],
    timeout: float,
) -> list[str]:
    """构造 curl 参数数组（不经过 shell，杜绝命令注入）。"""
    cmd: list[str] = ["curl", "-sS", "-m", str(int(timeout))]
    if method == "HEAD":
        cmd.append("-I")
    else:
        cmd.extend(["-D", "-", "-o", "/dev/null"])
    if follow_redirects:
        cmd.append("-L")
    cmd.extend(_validate_custom_headers(headers))
    cmd.extend(["-w", _W_TEMPLATE, "--", url])
    return cmd


def _parse_output(stdout: str) -> dict[str, object]:
    """解析 curl 输出：响应头 + 末尾元信息。"""
    status_lines: list[str] = []
    final_headers: dict[str, str] = {}
    meta_line: str | None = None

    raw_lines = stdout.splitlines()
    body_lines: list[str] = []
    for line in raw_lines:
        if line.startswith(_META_PREFIX):
            meta_line = line
        else:
            body_lines.append(line)

    last_status_idx = -1
    for i, line in enumerate(body_lines):
        if line.startswith("HTTP/"):
            last_status_idx = i
            status_lines.append(line.strip())

    if last_status_idx >= 0:
        for line in body_lines[last_status_idx + 1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                final_headers[name.strip()] = value.strip()

    result: dict[str, object] = {
        "status_chain": status_lines,
        "headers": final_headers,
    }

    if meta_line:
        payload = meta_line[len(_META_PREFIX):]
        parts = payload.split("|")
        if len(parts) >= 4:
            code, size, elapsed, effective_url = parts[0], parts[1], parts[2], parts[3]
            result["status_code"] = int(code) if code.isdigit() else None
            result["size_download"] = int(size) if size.isdigit() else 0
            result["time_total"] = elapsed
            result["effective_url"] = effective_url
    return result


def _runtime_from_ctx(ctx: RunContextWrapper[Any]) -> dict[str, Any]:
    """从 ctx.context 提取运行时依赖。"""
    inner = ctx.context if isinstance(ctx.context, dict) else {}
    return inner


def _exec_in_sandbox(session: Any, cmd: list[str], timeout: float) -> Any:
    """在线程池中执行 sandbox session 的 async exec 方法。

    SDK 的 function_tool sync 函数在线程池中运行，当前线程没有事件循环，
    所以可以安全地用 asyncio.run() 创建新事件循环来执行 async 方法。
    """
    async def _do_exec():
        return await session.exec(*cmd, timeout=timeout, shell=False)
    return asyncio.run(_do_exec())


@function_tool(strict_mode=False)
def http_check(
    ctx: RunContextWrapper[Any],
    url: str,
    method: str = "GET",
    follow_redirects: bool = False,
    headers: dict[str, str] | None = None,
) -> str:
    """对已授权的 Web 目标执行只读 HTTP 检查（GET/HEAD）。

    返回状态码、响应体大小、耗时、响应头，以及关键安全响应头是否缺失。
    只能访问授权白名单内的目标。

    Args:
        url: 要检查的完整 URL，必须以 http:// 或 https:// 开头，且命中授权白名单
        method: HTTP 方法，仅支持只读的 GET 或 HEAD，默认 GET
        follow_redirects: 是否跟随重定向（-L），默认 false
        headers: 可选的附加请求头，键为头名、值为头值（禁止换行）
    """
    rt = _runtime_from_ctx(ctx)
    sandbox_session = rt.get("sandbox_session")
    allowed_targets = rt.get("allowed_targets", ())
    command_timeout = rt.get("command_timeout", 30.0)

    if sandbox_session is None:
        return json.dumps({"ok": False, "error": "sandbox session 未初始化"}, ensure_ascii=False)

    url = url.strip()
    method = method.upper()
    headers = headers or {}

    target_origin = _origin(url)
    if target_origin is None:
        return json.dumps(
            {"ok": False, "error": f"URL 必须是 http/https 且含主机名：{url!r}"},
            ensure_ascii=False,
        )
    if method not in _READONLY_METHODS:
        return json.dumps(
            {"ok": False, "error": f"仅支持只读方法 {_READONLY_METHODS}，收到：{method}"},
            ensure_ascii=False,
        )

    allowed = _allowed_origins(allowed_targets)
    if target_origin not in allowed:
        return json.dumps(
            {"ok": False, "error": f"目标 {target_origin} 不在授权白名单内，已拒绝"},
            ensure_ascii=False,
        )

    try:
        cmd = _build_curl(
            url=url,
            method=method,
            follow_redirects=follow_redirects,
            headers=headers,
            timeout=command_timeout,
        )
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    # 通过 SDK sandbox session 执行 curl 命令
    try:
        result = _exec_in_sandbox(sandbox_session, cmd, command_timeout + 5)
    except Exception as exc:
        return json.dumps(
            {"ok": False, "url": url, "error": f"sandbox exec 异常：{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )

    stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else str(result.stdout)
    stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else str(result.stderr)
    exit_code = result.exit_code

    if exit_code != 0:
        return json.dumps(
            {
                "ok": False,
                "url": url,
                "error": (
                    f"curl 执行失败（exit={exit_code}）："
                    f"{stderr.strip() or stdout.strip()}"
                ),
            },
            ensure_ascii=False,
        )

    parsed = _parse_output(stdout)
    final_headers: dict[str, str] = parsed.get("headers", {})  # type: ignore[assignment]
    lower = {k.lower(): v for k, v in final_headers.items()}
    missing = [name for name in _SECURITY_HEADERS if name.lower() not in lower]

    data = {
        "ok": True,
        "url": url,
        "method": method,
        "status_code": parsed.get("status_code"),
        "status_chain": parsed.get("status_chain", []),
        "size_download": parsed.get("size_download", 0),
        "time_total": parsed.get("time_total"),
        "effective_url": parsed.get("effective_url"),
        "headers": final_headers,
        "location": lower.get("location"),
        "missing_security_headers": missing,
        "server": lower.get("server"),
        "content_type": lower.get("content-type"),
    }
    return json.dumps(data, ensure_ascii=False, default=str)
