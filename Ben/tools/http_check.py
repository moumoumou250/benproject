"""基础 HTTP 检查工具（第一阶段，只读、低风险）。

在受控执行容器内用 curl 完成：
- HTTP 连通性：最终状态码、响应体大小、耗时；
- 响应头检查：列出响应头，以及关键安全响应头是否缺失（仅作加固观察，
  缺安全头不认定为漏洞，也不单独写报告——与参考项目 vuln-report-format 一致）。

安全约束：
- 仅允许 http/https；目标 origin 必须命中授权白名单（ToolRuntime.allowed_targets），
  其余地址一律拒绝（容器默认不允许访问任意公网）；
- 仅允许 GET/HEAD（只读）；
- 命令以参数数组构造、不经过 shell，URL 与头值都不是 shell 文本；
- 自定义头名校验字符集、头值禁止 CRLF，防止头注入；
- 真实结果以执行器返回的 stdout/退出码为准，不凭空猜测。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from Ben.tools.registry import (
    ToolResult,
    ToolRuntime,
    ToolValidationError,
    ToolSpec,
)

# 标记行前缀：curl -w 输出的元信息，便于从 stdout 中切分
_META_PREFIX = "__BEN_META__"
# -w 模板：状态码|响应体字节数|总耗时|最终URL
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

# HTTP_CHECK_SPEC是ToolSpec的实例对象
HTTP_CHECK_SPEC = ToolSpec(
    name="http_check",
    description=(
        "对已授权的 Web 目标执行只读 HTTP 检查（GET/HEAD）：返回状态码、响应体大小、"
        "耗时、响应头，以及关键安全响应头是否缺失。用于连通性确认与响应头检查；"
        "只能访问授权白名单内的目标。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要检查的完整 URL，必须以 http:// 或 https:// 开头，且命中授权白名单",
            },
            "method": {
                "type": "string",
                "enum": list(_READONLY_METHODS),
                "description": "HTTP 方法，仅支持只读的 GET 或 HEAD，默认 GET",
                "default": "GET",
            },
            "follow_redirects": {
                "type": "boolean",
                "description": "是否跟随重定向（-L），默认 false",
                "default": False,
            },
            "headers": {
                "type": "object",
                "description": "可选的附加请求头，键为头名、值为头值（禁止换行）",
                "additionalProperties": {"type": "string"},
                "default": {},
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    handler=lambda args, runtime: http_check(args, runtime),
)


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
            raise ToolValidationError(f"非法请求头名：{name!r}")
        if any(ch in value for ch in "\r\n"):
            raise ToolValidationError(f"请求头 {name} 的值禁止包含回车/换行（防头注入）")
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
        # -I 发起 HEAD 并把响应头输出到 stdout
        cmd.append("-I")
    else:
        # -D - 把响应头输出到 stdout，-o /dev/null 丢弃响应体
        cmd.extend(["-D", "-", "-o", "/dev/null"])
    if follow_redirects:
        cmd.append("-L")
    cmd.extend(_validate_custom_headers(headers))
    cmd.extend(["-w", _W_TEMPLATE, "--", url])
    return cmd


def _parse_output(stdout: str) -> dict[str, object]:
    """解析 curl 输出：响应头 + 末尾元信息。

    支持重定向链（多组头）：记录每个状态行，最终响应头取最后一组。
    """
    status_lines: list[str] = []
    final_headers: dict[str, str] = {}
    meta_line: str | None = None

    # 当前是否处于"最后一组头"（最终响应）。从后往前找最后一个状态行更稳妥。
    raw_lines = stdout.splitlines()
    # 分离 meta 行
    body_lines: list[str] = []
    for line in raw_lines:
        if line.startswith(_META_PREFIX):
            meta_line = line
        else:
            body_lines.append(line)

    # 定位最后一个状态行的位置
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


def http_check(arguments: dict[str, object], runtime: ToolRuntime) -> ToolResult:
    """工具主逻辑：校验目标白名单 -> 容器内执行 curl -> 解析真实结果。"""
    url = str(arguments["url"]).strip()
    method = str(arguments.get("method", "GET")).upper()
    follow_redirects = bool(arguments.get("follow_redirects", False))
    headers = arguments.get("headers", {}) or {}

    target_origin = _origin(url)
    if target_origin is None:
        raise ToolValidationError(f"URL 必须是 http/https 且含主机名：{url!r}")
    if method not in _READONLY_METHODS:
        raise ToolValidationError(f"仅支持只读方法 {_READONLY_METHODS}，收到：{method}")
    if not isinstance(headers, dict):
        raise ToolValidationError("headers 必须是对象")

    allowed = _allowed_origins(runtime.allowed_targets)
    if target_origin not in allowed:
        raise ToolValidationError(
            f"目标 {target_origin} 不在授权白名单内（允许：{sorted(allowed)}），已拒绝"
        )

    cmd = _build_curl(
        url=url,
        method=method,
        follow_redirects=follow_redirects,
        headers=headers,  # type: ignore[arg-type]
        timeout=runtime.command_timeout,
    )
    exec_result = runtime.executor.run(cmd, timeout=runtime.command_timeout + 5)

    # curl 非零退出（网络不可达、超时等）：如实返回失败，不编造状态码。
    if not exec_result.ok:
        return ToolResult(
            name="http_check",
            ok=False,
            summary="",
            data={"url": url, "command": list(cmd)},
            error=(
                f"curl 执行失败（exit={exec_result.exit_code}）："
                f"{exec_result.stderr.strip() or exec_result.stdout.strip()}"
            ),
        )

    parsed = _parse_output(exec_result.stdout)
    final_headers: dict[str, str] = parsed.get("headers", {})  # type: ignore[assignment]
    lower = {k.lower(): v for k, v in final_headers.items()}
    missing = [name for name in _SECURITY_HEADERS if name.lower() not in lower]

    data = {
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

    # 给模型的简洁文本（全部来自真实响应）
    lines = [
        f"URL：{url}",
        f"状态码：{data['status_code']}（链路：{', '.join(data['status_chain']) or '无'}）",
        f"响应体大小：{data['size_download']} 字节；耗时：{data['time_total']}s",
    ]
    if data.get("location"):
        lines.append(f"Location：{data['location']}")
    if data.get("server"):
        lines.append(f"Server：{data['server']}")
    if data.get("content_type"):
        lines.append(f"Content-Type：{data['content_type']}")
    lines.append("已具备的安全头：" + (", ".join(
        n for n in _SECURITY_HEADERS if n.lower() in lower) or "无"))
    lines.append("缺失的安全头（仅加固建议，非漏洞）：" + (", ".join(missing) or "无"))

    return ToolResult(
        name="http_check",
        ok=True,
        summary="\n".join(lines),
        data=data,
    )
