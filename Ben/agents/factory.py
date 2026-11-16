"""构建 root / child Ben Agent（结构与函数签名对齐 strix 的 agents/factory.py）。

与 strix 的差异（Ben 架构约束，刻意保留）：
- 使用普通 ``Agent`` 而非 ``SandboxAgent``：DeepSeek/GLM 端点只支持
  chat_completions API，不能走 SandboxAgent 的 hosted tools 装配路径。
  Shell / Filesystem capability 手动 ``bind(sandbox_session)`` 后取出工具挂载，
  与 strix 的 ``_make_shell_configurator`` / ``_make_filesystem_configurator``
  等效；SDK 的 CustomTool（apply_patch）经 ``_custom_tool_as_function_tool``
  转成 FunctionTool 以适配 chat_completions 路由。
- ``_wrap_exec_command`` 前置授权白名单校验：命令中提取的每个主机名/IP
  都必须在 ``allowed_targets`` 内，否则拒绝执行（Ben 特有硬约束）。

对齐的 strix 结构（strix/agents/factory.py）：
- ``_BASE_TOOLS`` / ``_EXTRA_TOOLS`` / ``register_agent_tools``
- ``build_agent``（对应 ``build_strix_agent``）：root 挂 ``finish_scan``，
  child 挂 ``agent_finish``；``tool_use_behavior=_finish_tool_use_behavior``
- ``make_child_factory``：runner 持有的子 agent 工厂，闭包捕获运行级参数
- 工具包装链：``_with_coerced_arguments`` → ``_with_strictness``
  → ``_with_bounded_result``；shell 工具另加 ``_wrap_exec_command`` /
  ``_wrap_write_stdin`` / ``_function_tool_with_error_result``

已对齐的工具族含 threat_model / web_search / wait_for_user /
view_image / apply_patch（patch），以及 mcp 发现/派发五件套
（list_mcps / search_mcp_tools / get_mcp_tool_schema / describe_mcp /
call_mcp，连接本身由 runner 装配进运行上下文）。
"""

from __future__ import annotations

import dataclasses
import inspect
import ipaddress
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents import Agent
from agents.agent import ToolsToFinalOutputResult
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.session.pty_types import PTY_YIELD_TIME_MS_MAX
from agents.tool import CustomTool, FunctionTool, Tool
from pydantic import ValidationError

from Ben.agents.prompt import render_system_prompt
from Ben.config import load_settings
from Ben.config.tool_call_arguments import describe_malformed_arguments
from Ben.tools.agents_graph.tools import (
    agent_finish,
    create_agent,
    send_message_to_agent,
    stop_agent,
    view_agent_graph,
    wait_for_agents,
)
from Ben.tools.coverage.tools import list_coverage, record_coverage, update_coverage
from Ben.tools.finish.tool import finish_scan
from Ben.tools.load_skill.tool import load_skill
from Ben.rag.tools import search_knowledge
from Ben.tools.mcp.agent_tools import (
    call_mcp,
    describe_mcp,
    get_mcp_tool_schema,
    list_mcps,
    search_mcp_tools,
)
from Ben.tools.notes.tools import (
    create_note,
    delete_note,
    get_note,
    list_notes,
    update_note,
)
from Ben.tools.nullish import is_nullish
from Ben.tools.output_store import bound_and_store, bound_text
from Ben.tools.proxy.tools import (
    list_requests,
    list_sitemap,
    repeat_request,
    scope_rules,
    view_request,
    view_sitemap_entry,
)
from Ben.tools.reporting.tool import (
    create_dependency_report,
    create_vulnerability_report,
    delete_vulnerability_report,
    get_report,
    list_reports,
    update_vulnerability_report,
)
from Ben.tools.thinking.tool import think
from Ben.tools.threat_model.tools import (
    amend_threat_model,
    get_threat_model,
    save_threat_model,
)
from Ben.tools.todo.tools import (
    create_todo,
    delete_todo,
    list_todos,
    mark_todo_done,
    mark_todo_pending,
    update_todo,
)
from Ben.tools.wait_for_user.tool import wait_for_user
from Ben.tools.web_search.tool import web_get_contents, web_search

if TYPE_CHECKING:
    from agents import RunContextWrapper
    from agents.tool import FunctionToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具结果体积约束（对齐 strix factory 的 output_store 集成）
# ---------------------------------------------------------------------------


def _tool_output_limits() -> tuple[int, int]:
    context = load_settings().context
    return context.tool_output_max_lines, context.tool_output_max_bytes


async def _bound_result(result: Any) -> Any:
    if not isinstance(result, str):
        return result
    max_lines, max_bytes = _tool_output_limits()
    return await bound_and_store(result, max_lines=max_lines, max_bytes=max_bytes)


def _format_tool_error(exc: Exception) -> str:
    message = str(exc) or exc.__class__.__name__
    max_lines, max_bytes = _tool_output_limits()
    return bound_text(message, max_lines=max_lines, max_bytes=max_bytes)


def _with_bounded_result(tool: FunctionTool) -> FunctionTool:
    """Cap a tool's result size before it enters history (idempotent).

    超大结果经 output_store 头尾截断，完整内容 spill 到沙箱
    /workspace/.tool-output/<id>.txt（spill writer 由 runner 在 session
    就绪后经 configure_spill_writer 注入）。
    """
    if getattr(tool, "_ben_bounded", False):
        return tool
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        return await _bound_result(await invoke_tool(ctx, raw_input))

    tool.on_invoke_tool = invoke
    tool._ben_bounded = True  # type: ignore[attr-defined]
    return tool


# ---------------------------------------------------------------------------
# SDK CustomTool → FunctionTool 转换（对齐 strix factory；apply_patch 等
# Responses custom tool 在 chat_completions 路由下必须以 function tool 暴露）
# ---------------------------------------------------------------------------

_CUSTOM_TOOL_INPUT_FIELD_BY_NAME = {
    "apply_patch": "patch",
}
_DEFAULT_CUSTOM_TOOL_INPUT_FIELD = "input"


def _custom_tool_input_field(tool: CustomTool) -> str:
    return _CUSTOM_TOOL_INPUT_FIELD_BY_NAME.get(tool.name, _DEFAULT_CUSTOM_TOOL_INPUT_FIELD)


def _raw_input_schema(tool: CustomTool) -> dict[str, Any]:
    input_field = _custom_tool_input_field(tool)
    return {
        "type": "object",
        "properties": {
            input_field: {
                "type": "string",
                "description": (
                    f"Complete `{tool.name}` payload. Follow the tool description exactly."
                ),
            },
        },
        "required": [input_field],
        "additionalProperties": False,
    }


def _extract_custom_input(tool: CustomTool, raw_input: str | dict[str, Any]) -> str:
    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
        except json.JSONDecodeError:
            return ""
    else:
        parsed = raw_input
    value = parsed.get(_custom_tool_input_field(tool))
    return value if isinstance(value, str) else ""


def _custom_tool_as_function_tool(tool: CustomTool) -> FunctionTool:
    async def invoke(ctx: Any, raw_input: str) -> Any:
        custom_input = _extract_custom_input(tool, raw_input)
        if not custom_input:
            return f"`{_custom_tool_input_field(tool)}` must be a non-empty string."
        try:
            return await _bound_result(await tool.on_invoke_tool(ctx, custom_input))
        except Exception as exc:  # noqa: BLE001 - matches SDK CustomTool error-as-result behavior.
            logger.debug("Tool %s failed; returning error as result", tool.name, exc_info=True)
            return _format_tool_error(exc)

    needs_approval = tool.runtime_needs_approval()
    function_needs_approval: bool | Callable[[Any, dict[str, Any], str], Awaitable[bool]]
    if callable(needs_approval):

        async def approve(ctx: Any, args: dict[str, Any], call_id: str) -> bool:
            result = needs_approval(ctx, _extract_custom_input(tool, args), call_id)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)

        function_needs_approval = approve
    else:
        function_needs_approval = needs_approval

    return FunctionTool(
        name=tool.name,
        description=(
            f"{tool.description}\n\n"
            f"Pass the complete `{tool.name}` payload in `{_custom_tool_input_field(tool)}`."
        ),
        params_json_schema=_raw_input_schema(tool),
        on_invoke_tool=invoke,
        strict_json_schema=False,
        needs_approval=function_needs_approval,
    )


# ---------------------------------------------------------------------------
# 参数矫正（对齐 strix 的 _with_coerced_arguments 链）
# ---------------------------------------------------------------------------


def _schema_types(spec: dict[str, Any]) -> set[str]:
    types: set[str] = set()
    raw = spec.get("type")
    if isinstance(raw, str):
        types.add(raw)
    elif isinstance(raw, list):
        types.update(t for t in raw if isinstance(t, str))
    for variant in spec.get("anyOf") or ():
        if isinstance(variant, dict):
            types |= _schema_types(variant)
    types.discard("null")
    return types


def _allows_null(spec: dict[str, Any]) -> bool:
    raw = spec.get("type")
    if raw == "null" or (isinstance(raw, list) and "null" in raw):
        return True
    return any(
        isinstance(variant, dict) and _allows_null(variant) for variant in spec.get("anyOf") or ()
    )


def _is_nullable(key: str, spec: dict[str, Any], schema: dict[str, Any]) -> bool:
    """Whether ``key`` may be ``None``（对齐 strix）。"""
    if _allows_null(spec):
        return True
    required = schema.get("required")
    return isinstance(required, list) and key not in required


def _decode_structured(value: str, types: set[str]) -> Any:
    stripped = value.strip()
    if not stripped:
        # 模型对 list/dict 参数的"无值"表达；给空容器使其通过校验
        return [] if "array" in types else {}
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return value
    wanted = list if "array" in types else dict
    return decoded if isinstance(decoded, wanted) else value


def _coerce_argument(value: Any, spec: dict[str, Any], *, nullable: bool = False) -> Any:
    if value is None:
        return value
    if nullable and is_nullish(value):
        # 模型的"无值"替身；作为过滤器时不匹配任何内容
        return None
    types = _schema_types(spec)
    if not types:
        return value
    if isinstance(value, list | dict) and "string" in types and not types & {"array", "object"}:
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and types & {"array", "object"} and "string" not in types:
        return _decode_structured(value, types)
    return value


# 只有查询类工具做 nullish 矫正：写字段可能真的需要 "null" 字面量
_QUERY_TOOL_PREFIXES = ("list_", "search_", "view_", "get_")


def _coerce_arguments(raw_input: str, schema: dict[str, Any], *, nullish: bool = False) -> str:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return raw_input
    try:
        payload = json.loads(raw_input) if raw_input else None
    except json.JSONDecodeError:
        return raw_input
    if not isinstance(payload, dict):
        return raw_input

    changed = False
    for key, value in payload.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        coerced = _coerce_argument(
            value, spec, nullable=nullish and _is_nullable(key, spec, schema)
        )
        if coerced is not value:
            payload[key] = coerced
            changed = True

    if not changed:
        return raw_input
    return json.dumps(payload, ensure_ascii=False)


def _with_coerced_arguments(tool: FunctionTool) -> FunctionTool:
    if getattr(tool, "_ben_coerced", False):
        return tool
    invoke_tool = tool.on_invoke_tool
    schema = tool.params_json_schema
    nullish = tool.name.startswith(_QUERY_TOOL_PREFIXES)

    async def invoke(ctx: Any, raw_input: str) -> Any:
        malformed = describe_malformed_arguments(tool.name, raw_input)
        if malformed is not None:
            logger.debug("Tool %s got malformed arguments; asking the model to re-issue", tool.name)
            return malformed
        return await invoke_tool(ctx, _coerce_arguments(raw_input, schema, nullish=nullish))

    tool.on_invoke_tool = invoke
    tool._ben_coerced = True  # type: ignore[attr-defined]
    return tool


def _with_strictness(tool: FunctionTool, strict_schemas: bool) -> FunctionTool:
    """路由不支持 strict JSON-schema 时降级（工具功能不变）。

    返回副本，共享的工具单例保持其声明的模式。
    """
    if strict_schemas or not tool.strict_json_schema:
        return tool
    return dataclasses.replace(tool, strict_json_schema=False)


def _function_tool_with_error_result(tool: FunctionTool) -> FunctionTool:
    """工具异常不抛出，一律作为结果返回给模型（chat_completions 模式）。"""
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        try:
            return await _bound_result(await invoke_tool(ctx, raw_input))
        except Exception as exc:  # noqa: BLE001 - 工具错误应对模型可见
            logger.debug("Tool %s failed; returning error as result", tool.name, exc_info=True)
            return _format_tool_error(exc)

    tool.on_invoke_tool = invoke
    return tool


# ---------------------------------------------------------------------------
# 授权白名单（Ben 特有硬约束，原 Ben/tools/exec_command 的校验逻辑）
# ---------------------------------------------------------------------------

# 回环/本机地址视为沙箱内部操作，不做白名单校验
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$"
)
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

# 网络客户端命令：命中后才施加只读方法与间接引用校验。
# 末尾用「后接分隔符或行尾」而非 \b：\b 会把 `http.server`、`http.py`
# 这类文件名/模块名误判成网络客户端（`python3 -m http.server` 曾被误拒）。
_NET_CLIENT_RE = re.compile(
    r"(?i)(?:^|[\s;|&(])"
    r"(?:curl|wget|https?|nc|ncat|netcat|socat|nmap|masscan|ping|ping6|dig|nslookup|host|"
    r"whois|traceroute|ssh|scp|sftp|telnet|ftp|smbclient|nikto|sqlmap|ffuf|gobuster|"
    r"dirb|nuclei|wfuzz|httpx|openssl)"
    r"(?=[\s;|&)]|$)"
)

# 这些标志的下一个参数是本地文件名（输出文件/输入清单），不是网络目标。
# 只收「必然带一个文件名参数」的标志：curl 的 -f/-i/-w 是布尔标志，
# 若收进来会把紧跟其后的 URL 一起跳过，形成绕过。
_FILE_ARG_FLAGS = frozenset(
    {
        "-o", "-ox", "-og", "-oa", "-on", "-oi", "-oj", "-oh", "-os", "-oxml",
        "-il", "--output", "--output-file", "--log", "--logfile",
    }
)

# 显式 HTTP 方法标志：短标志 -X 大小写敏感（curl 的 -x 是代理），
# 长标志 --request 大小写不敏感。
_METHOD_SHORT_FLAG_RE = re.compile(r"(?:^|\s)-X\s*([A-Z]+)\b")
_METHOD_LONG_FLAG_RE = re.compile(r"(?i)(?:^|\s)--request(?:\s*=\s*|\s+)([A-Z]+)\b")

# 携带请求体/上传的标志（无论方法如何都视为写操作）。
# 短标志必须大小写敏感：curl 的 -d 是 data（写）、-D 是 dump-header（读）；
# -F 是 form（写）、-f 是 fail（读）；-T 是上传、-t 是 telnet-option。
# 大小写不敏感会把 `curl -D -` 这类纯读取命令误拒。
_PAYLOAD_SHORT_FLAG_RE = re.compile(r"(?:^|\s)(?:-d\b|-F\b|-T\b)")
_PAYLOAD_LONG_FLAG_RE = re.compile(
    r"(?i)(?:^|\s)(?:--data(?:-raw|-binary|-ascii|-urlencode)?(?:\s|=|$)"
    r"|--form(?:-string)?(?:\s|=|$)|--upload-file(?:\s|=|$)"
    r"|--json(?:\s|=|$)|--post-data(?:\s|=|$)|--post-file(?:\s|=|$))"
)

# shell 间接引用（变量/命令替换）：出现时无法静态确定真实目标
_INDIRECTION_RE = re.compile(r"\$\{|\$\(|\$[A-Za-z_]|`")

# 只读阶段允许的 HTTP 方法
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _normalize_host(host: str) -> str:
    """归一化主机字面量：整数/十六进制 IP 形式转为点分十进制。

    curl 接受 ``http://2130706433/``、``http://0x7f000001/`` 等变形，
    不归一化会绕过 IPv4 白名单比对。
    """
    host = host.lower().strip("[]")
    try:
        value = int(host, 0)
    except ValueError:
        return host
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        return host


def _segment_uses_net_client(segment: str) -> bool:
    tokens = [tok for tok in re.split(r"\s+", segment) if tok]
    return any(_is_net_client_name(tok.strip("\"';")) for tok in tokens)


def _extract_hosts_from_segment(segment: str, *, bare_domains: bool) -> set[str]:
    """从一个简单命令里取 URL 主机、IPv4，以及（仅网络客户端命令）裸域名。

    裸域名不从相邻的 python/echo 片段里取。否则 ``curl`` 与
    ``line.lower()``、``echo robots.txt`` 写在同一段脚本里时，
    文件名和属性访问会被当成未授权主机整段拒绝。
    URL 仍从每个片段提取，``echo http://evil.com`` 不会因此放行。
    """
    from urllib.parse import urlsplit

    hosts: set[str] = set()
    prev_token = ""
    for raw_token in re.split(r"\s+", segment):
        token = raw_token.strip("\"';|&()<>")
        if "=" in token and "://" not in token.split("=", 1)[0]:
            token = token.split("=", 1)[1].strip("\"'")
        if not token:
            prev_token = ""
            continue
        # `-oX out.xml` 一类：文件名不是网络目标。
        # ``from html.parser`` / ``import urllib.parse`` / ``python -m http.server``
        # 的点号 token 是模块名，不是主机。
        prior = prev_token
        skip_as_file = prev_token.lower() in _FILE_ARG_FLAGS
        prev_token = token
        if skip_as_file and "://" not in token:
            continue
        if "://" in token:
            try:
                hostname = urlsplit(token).hostname
            except ValueError:
                continue
            if hostname:
                hosts.add(_normalize_host(hostname))
            continue
        if "/" in token:  # 路径（含 /）不是主机
            continue
        if "@" in token:  # userinfo（ssh user@host）不参与主机判定
            token = token.rsplit("@", 1)[1]
        if token.count(":") == 1:  # host:port 形式
            maybe_host, _, port = token.partition(":")
            if port.isdigit():
                token = maybe_host
        if _IPV4_RE.match(token):
            hosts.add(_normalize_host(token))
        elif (
            bare_domains
            and prior not in {"from", "import", "-m"}
            and _DOMAIN_RE.match(token)
        ):
            hosts.add(token.lower())
    return hosts


def _extract_hosts(command: str, *, net_context: bool = True) -> set[str]:
    """从命令字符串中提取全部网络目标（URL 主机、域名、IPv4）。

    URL 一律经 urlsplit 取 hostname：userinfo（``http://a@evil.com/``）、
    端口、路径不会干扰主机判定；整数/十六进制 IP 归一化为点分十进制。

    ``net_context=False``（命令未调用任何网络客户端）时只提取 URL 与 IPv4，
    不把裸点号 token 当域名——否则 ``python main.py``、``cat notes.txt``、
    ``nmap -oX out.xml`` 里的文件名会被误判成未授权目标而整条命令被拒。
    有网络客户端时，裸域名也只从该客户端所在的简单命令里取。

    裸主机名（无点号的 docker 内部服务名，如 juice-shop）不匹配域名规则，
    不会被误判为外网目标。
    """
    if not net_context:
        return _extract_hosts_from_segment(command, bare_domains=False)
    hosts: set[str] = set()
    for segment in _simple_commands(command):
        hosts.update(
            _extract_hosts_from_segment(
                segment, bare_domains=_segment_uses_net_client(segment)
            )
        )
    return hosts


def _simple_commands(command: str) -> list[str]:
    """按 ``;``、换行、管道和 ``&&`` 切开简单命令，引号内的分隔符不算。

    ``cut -d'|'`` 的竖线在引号里，不能把一条 cut 切成两段。
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        char = command[i]
        if quote is not None:
            buf.append(char)
            if char == quote and (quote == "'" or command[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            i += 1
            continue
        if char in {";", "\n", "|"} or (
            char == "&" and i + 1 < len(command) and command[i + 1] == "&"
        ):
            parts.append("".join(buf))
            buf = []
            i += 2 if char == "&" else 1
            continue
        buf.append(char)
        i += 1
    parts.append("".join(buf))
    return [part for part in parts if part.strip()]


def _is_net_client_name(token: str) -> bool:
    return _NET_CLIENT_RE.search(f" {token} ") is not None


def _payload_flag(command: str) -> re.Match[str] | None:
    """写负载标志只算在调用了网络客户端的那一条简单命令上。

    ``curl … | cut -d'|'``、``tr -d`` 里的 ``-d`` 是分隔符/删除字符，
    不是 curl 的 ``--data``。整段原文一次匹配会把这种只读命令误拒。
    """
    for segment in _simple_commands(command):
        tokens = [tok for tok in re.split(r"\s+", segment) if tok]
        if not any(_is_net_client_name(tok.strip("\"';")) for tok in tokens):
            continue
        payload = _PAYLOAD_SHORT_FLAG_RE.search(segment) or _PAYLOAD_LONG_FLAG_RE.search(segment)
        if payload:
            return payload
    return None


def _check_command_readonly(command: str) -> str | None:
    """只读守卫（Ben 特有硬约束）：命中网络客户端且携带写语义时返回拒绝原因。

    仅在命令调用网络客户端（curl/wget/http/nc/socat）时生效：
    - 显式方法标志限 GET/HEAD/OPTIONS；
    - 该网络客户端自己的请求体/上传标志（-d/--data/-F/-T/--json/--post-*）一律拒绝。
    """
    if not _NET_CLIENT_RE.search(command):
        return None
    for match in _METHOD_SHORT_FLAG_RE.finditer(command):
        method = match.group(1).upper()
        if method not in _READ_METHODS:
            return (
                f"命令使用非只读 HTTP 方法 {method}"
                f"（当前阶段仅允许 {'/'.join(sorted(_READ_METHODS))}）"
            )
    for match in _METHOD_LONG_FLAG_RE.finditer(command):
        method = match.group(1).upper()
        if method not in _READ_METHODS:
            return (
                f"命令使用非只读 HTTP 方法 {method}"
                f"（当前阶段仅允许 {'/'.join(sorted(_READ_METHODS))}）"
            )
    payload = _payload_flag(command)
    if payload:
        return (
            f"命令包含写负载标志 {payload.group(0).strip()!r}"
            "（只读阶段禁止携带请求体/上传内容）"
        )
    return None


def _allowed_hostnames(allowed_targets: tuple[str, ...]) -> set[str]:
    """白名单配置 -> 主机名集合（小写）。

    兼容两种写法：``http://host:port/path`` 与裸的 ``host`` / ``host:port``。
    后者以前会被 urlsplit 当成 path 而丢掉，导致白名单里写了域名却一条命令
    都放不出去（扫描全程"未授权目标"，看起来像模型不干活）。
    """
    from urllib.parse import urlsplit

    hosts: set[str] = set()
    for target in allowed_targets:
        raw = str(target or "").strip()
        if not raw:
            continue
        candidate = raw if "://" in raw else f"//{raw}"
        try:
            hostname = urlsplit(candidate).hostname
        except ValueError:
            continue
        if not hostname:
            continue
        hostname = hostname.lower()
        if not any(char.isalnum() for char in hostname):
            continue  # "." / "-" 这类来自本地路径的残渣
        hosts.add(hostname)
    return hosts


def _check_command_allowed(command: str, allowed_targets: tuple[str, ...]) -> str | None:
    """命令授权校验（Ben 特有硬约束）；返回拒绝原因，None = 通过。

    三道闸门按序执行：
      1. 只读守卫：网络客户端不得携带写方法/写负载；
      2. 间接引用：含变量/命令替换时无法静态确定真实目标，一律拒绝；
      3. 主机白名单：命令中每个主机名/IP 都必须在白名单内。
    """
    reason = _check_command_readonly(command)
    if reason:
        return reason

    net_context = bool(_NET_CLIENT_RE.search(command))
    # 只在网络客户端那一条简单命令上查变量。python 片段里的 `$` 不会改 curl 的主机。
    if any(_INDIRECTION_RE.search(segment) for segment in _simple_commands(command) if _segment_uses_net_client(segment)):
        return "命令包含 shell 变量/命令替换，无法静态确认网络目标，请展开后重试"

    hosts = _extract_hosts(command, net_context=net_context) - _LOCAL_HOSTS
    if not hosts:
        return None
    allowed = _allowed_hostnames(allowed_targets)
    unauthorized = {h for h in hosts if h not in allowed}
    if unauthorized:
        return (
            f"命令包含未授权目标 {sorted(unauthorized)}，已拒绝执行。"
            f"授权范围：{sorted(allowed)}"
        )
    return None


# ---------------------------------------------------------------------------
# strix 风格的 shell 工具包装
# ---------------------------------------------------------------------------

_CHARS_ESCAPE_RE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[0abtnvfr\\])")
_CHARS_ESCAPE_MAP = {
    "\\\\": "\\",
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "\r",
    "\\0": "\x00",
    "\\a": "\x07",
    "\\b": "\x08",
    "\\v": "\x0b",
    "\\f": "\x0c",
}


def _decode_chars_escape(s: str) -> str:
    if "\\" not in s:
        return s

    def sub(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in _CHARS_ESCAPE_MAP:
            return _CHARS_ESCAPE_MAP[token]
        if token.startswith(("\\u", "\\x")):
            return chr(int(token[2:], 16))
        return token

    return _CHARS_ESCAPE_RE.sub(sub, s)


def _format_validation_error(tool_name: str, exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return f"{tool_name}: invalid arguments — " + "; ".join(parts)


def _apply_shell_output_cap(parsed: dict[str, Any]) -> None:
    """Clamp the SDK shell tools' ``max_output_tokens`` to the configured
    ceiling; a smaller explicit value is respected."""
    ceiling = load_settings().context.tool_output_max_tokens
    requested = parsed.get("max_output_tokens")
    parsed["max_output_tokens"] = (
        ceiling if not isinstance(requested, int) or requested > ceiling else requested
    )


_EXEC_YIELD_CAP_S = PTY_YIELD_TIME_MS_MAX // 1000

_EXEC_COMMAND_DESCRIPTION_NOTE = (
    f"The wait for output is capped at {_EXEC_YIELD_CAP_S} s whatever yield_time_ms says. "
    "A non-TTY command still running at the yield cannot be read later (write_stdin fails "
    "on it and its output is lost): run anything longer in the background with its output "
    "redirected to a file (`cmd > /tmp/out.log 2>&1 &`, then read the log), or start it with "
    'tty=true and poll with write_stdin(session_id=<id>, chars="").'
)

_RUNNING_NON_TTY_NOTICE = (
    "Note: this non-TTY process cannot be polled and its later output is lost. Rerun it in "
    "the background (`cmd > /tmp/out.log 2>&1 &`, then read the log) or with tty=true."
)

_PROCESS_RUNNING_RE = re.compile(r"^Process running with session ID \d+$", re.MULTILINE)
_OUTPUT_HEADER_RE = re.compile(r"^Output:\n", re.MULTILINE)


def _annotate_running_non_tty(parsed: dict[str, Any], result: Any) -> Any:
    if parsed.get("tty") or not isinstance(result, str):
        return result
    header = _OUTPUT_HEADER_RE.search(result)
    head_end = len(result) if header is None else header.start()
    if _PROCESS_RUNNING_RE.search(result, 0, head_end) is None:
        return result
    notice = _RUNNING_NON_TTY_NOTICE
    requested = parsed.get("yield_time_ms")
    if isinstance(requested, int | float) and requested > PTY_YIELD_TIME_MS_MAX:
        notice = (
            f"Note: yield_time_ms={int(requested)} was capped at {PTY_YIELD_TIME_MS_MAX} "
            f"({_EXEC_YIELD_CAP_S} s). " + notice.removeprefix("Note: ")
        )
    if header is None:
        return f"{result}\n{notice}"
    return f"{result[: header.end()]}{notice}\n{result[header.end() :]}"


async def _run_one_shot_shell(tool: Any, parsed: dict[str, Any]) -> str | None:
    """Run a non-TTY command through the SDK's one-shot exec path.

    The SDK's PTY transport loses all output for ``tty=False`` on some hosts
    (observed on Windows + Docker Desktop: the process exits with the right
    code but the reader thread yields no bytes), which would hand the model an
    empty result for every command. Non-TTY commands therefore go through
    ``BaseSandboxSession.exec`` — the same fallback the SDK itself uses for
    sandboxes without PTY support — and TTY requests keep the PTY tool.
    """
    session = getattr(tool, "session", None)
    if session is None:
        return None

    from agents.sandbox.capabilities.tools import shell_tool as _shell_tool
    from agents.sandbox.errors import ExecTimeoutError

    args = _shell_tool.ExecCommandArgs.model_validate(parsed)
    timeout_s = args.yield_time_ms / 1000
    start = time.perf_counter()
    wrapped = _shell_tool._resolve_workdir_command(
        session=session, command=args.cmd, workdir=args.workdir
    )
    shell = _shell_tool._resolve_shell(args.shell, args.login)
    try:
        output, exit_code, original_tokens = await _shell_tool._run_one_shot_exec(
            session=session,
            command=wrapped,
            timeout_s=timeout_s,
            shell=shell,
            max_output_tokens=args.max_output_tokens,
            user=getattr(tool, "user", None),
        )
    except (ExecTimeoutError, TimeoutError):
        output = f"Command timed out after {timeout_s:.3f} seconds."
        exit_code, original_tokens = None, None
    return _shell_tool._format_response(
        output=output,
        wall_time_seconds=time.perf_counter() - start,
        exit_code=exit_code,
        process_id=None,
        original_token_count=original_tokens,
    )


def _wrap_exec_command(tool: FunctionTool, *, allowed_targets: tuple[str, ...]) -> FunctionTool:
    """对齐 strix _wrap_exec_command；前置 Ben 的授权白名单校验。"""
    invoke_tool = tool.on_invoke_tool
    tool.description = f"{tool.description.rstrip()} {_EXEC_COMMAND_DESCRIPTION_NOTE}"

    async def invoke(ctx: Any, raw_input: str) -> Any:
        try:
            parsed = json.loads(raw_input)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            cmd = parsed.get("cmd")
            if isinstance(cmd, str):
                denial = _check_command_allowed(cmd, allowed_targets)
                if denial:
                    return json.dumps(
                        {"ok": False, "error": denial},
                        ensure_ascii=False,
                    )
            if "shell" not in parsed:
                parsed["shell"] = "bash"
            _apply_shell_output_cap(parsed)
            raw_input = json.dumps(parsed)
        try:
            if isinstance(parsed, dict) and not parsed.get("tty"):
                one_shot = await _run_one_shot_shell(tool, parsed)
                if one_shot is not None:
                    return one_shot
            result = await invoke_tool(ctx, raw_input)
        except ValidationError as exc:
            return _format_validation_error(tool.name, exc)
        if isinstance(parsed, dict):
            return _annotate_running_non_tty(parsed, result)
        return result

    tool.on_invoke_tool = invoke
    return tool


def _wrap_write_stdin(tool: FunctionTool) -> FunctionTool:
    """对齐 strix _wrap_write_stdin：chars 转义解码 + 输出上限。"""
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        try:
            parsed = json.loads(raw_input)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            if isinstance(parsed.get("chars"), str):
                parsed["chars"] = _decode_chars_escape(parsed["chars"])
            _apply_shell_output_cap(parsed)
            raw_input = json.dumps(parsed)
        try:
            return await invoke_tool(ctx, raw_input)
        except ValidationError as exc:
            return _format_validation_error(tool.name, exc)

    tool.on_invoke_tool = invoke
    return tool


def _configure_shell_tools(
    toolset: Any, *, allowed_targets: tuple[str, ...], strict_schemas: bool = True
) -> None:
    """对齐 strix _configure_shell_tools：逐个包装 SDK 生成的 shell 工具。

    Ben 恒为 chat_completions 路由，因此总是套 ``_function_tool_with_error_result``。
    """
    for name, tool in vars(toolset).items():
        if not isinstance(tool, FunctionTool):
            continue
        wrapped = _with_strictness(_with_coerced_arguments(tool), strict_schemas)
        if tool.name == "exec_command":
            wrapped = _wrap_exec_command(wrapped, allowed_targets=allowed_targets)
        elif tool.name == "write_stdin":
            wrapped = _wrap_write_stdin(wrapped)
        wrapped = _function_tool_with_error_result(wrapped)
        setattr(toolset, name, wrapped)


def _configure_filesystem_tools(
    toolset: Any, *, strict_schemas: bool = True, supports_images: bool = True
) -> None:
    """对齐 strix _configure_filesystem_tools：逐个包装 SDK 生成的文件系统工具。

    Ben 恒为 chat_completions 路由：CustomTool（apply_patch）转成
    FunctionTool，FunctionTool（view_image）走常规包装链；模型不支持
    视觉时禁用 view_image。
    """
    for name, tool in vars(toolset).items():
        if isinstance(tool, CustomTool):
            setattr(toolset, name, _custom_tool_as_function_tool(tool))
        elif isinstance(tool, FunctionTool):
            setattr(
                toolset,
                name,
                _function_tool_with_error_result(
                    _with_strictness(_with_coerced_arguments(tool), strict_schemas)
                ),
            )
    if not supports_images:
        toolset.view_image.is_enabled = False


# ---------------------------------------------------------------------------
# 生命周期工具结束判定（对齐 strix 的 tool_use_behavior）
# ---------------------------------------------------------------------------

# 通过"停泊"而非结束扫描来交出控制权的工具
_PARKING_TOOLS: frozenset[str] = frozenset({"wait_for_user", "wait_for_agents"})

# 完成标志键：内置结束工具各用其一；自定义 finish_tool（如 finish_pr_review）
# 用任一即可——否则自定义结束工具挂上了却永远不会终止运行。
_COMPLETION_KEYS: tuple[str, ...] = (
    "scan_completed",
    "agent_completed",
    "review_completed",
)


def _lifecycle_tool_completed(tool_name: str, output: Any) -> bool:
    if tool_name == "agent_finish":
        keys: tuple[str, ...] = ("agent_completed",)
    elif tool_name == "finish_scan":
        keys = ("scan_completed",)
    elif tool_name.startswith("finish_"):
        # 自定义结束工具：接受任一完成标志
        keys = _COMPLETION_KEYS
    else:
        return False

    if not isinstance(output, str):
        return False
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(parsed, dict)
        and parsed.get("success")
        and any(parsed.get(key) for key in keys)
    )


def _wait_tool_parked(tool_name: str, output: Any) -> bool:
    if tool_name not in _PARKING_TOOLS or not isinstance(output, str):
        return False
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(parsed, dict)
        and parsed.get("success")
        and parsed.get("wait_outcome") == "waiting"
    )


def _finish_tool_use_behavior(
    ctx: RunContextWrapper[Any],
    tool_results: list[FunctionToolResult],
) -> ToolsToFinalOutputResult:
    """Stop only after a lifecycle tool reports successful completion."""
    interactive = (
        bool(ctx.context.get("interactive", False)) if isinstance(ctx.context, dict) else False
    )
    for tool_result in tool_results:
        if _lifecycle_tool_completed(tool_result.tool.name, tool_result.output):
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=tool_result.output,
            )
        if interactive and _wait_tool_parked(tool_result.tool.name, tool_result.output):
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output=tool_result.output,
            )
    return ToolsToFinalOutputResult(is_final_output=False, final_output=None)


# ---------------------------------------------------------------------------
# 工具集（对齐 strix 的 _BASE_TOOLS / _EXTRA_TOOLS）
# ---------------------------------------------------------------------------

_COVERAGE_TOOLS: tuple[Tool, ...] = (record_coverage, update_coverage, list_coverage)
_FINISH_SCAN_COVERAGE_STEP = re.compile(
    r"\n5\. \*\*Coverage reconciliation\.\*\*.*?(?=\n\n)", re.DOTALL
)


def _without_coverage_step(tool: FunctionTool) -> FunctionTool:
    description = _FINISH_SCAN_COVERAGE_STEP.sub("", tool.description)
    return dataclasses.replace(tool, description=description)


# 顺序对齐 strix _BASE_TOOLS（scope_rules 后挂 MCP 发现/派发五件套）。
_BASE_TOOLS: tuple[Tool, ...] = (
    think,
    load_skill,
    search_knowledge,
    create_todo,
    list_todos,
    update_todo,
    mark_todo_done,
    mark_todo_pending,
    delete_todo,
    create_note,
    list_notes,
    get_note,
    update_note,
    delete_note,
    record_coverage,
    update_coverage,
    list_coverage,
    get_threat_model,
    save_threat_model,
    amend_threat_model,
    web_search,
    web_get_contents,
    create_vulnerability_report,
    create_dependency_report,
    update_vulnerability_report,
    delete_vulnerability_report,
    list_reports,
    get_report,
    list_requests,
    view_request,
    repeat_request,
    list_sitemap,
    view_sitemap_entry,
    scope_rules,
    list_mcps,
    search_mcp_tools,
    get_mcp_tool_schema,
    describe_mcp,
    call_mcp,
    view_agent_graph,
    send_message_to_agent,
    wait_for_agents,
    create_agent,
    stop_agent,
)


# 运行时后端注册的额外工具（对齐 strix：在首次 build_agent 前注册，
# root 与 child 都会获得；追加在基础工具之后、生命周期工具之前）。
_EXTRA_TOOLS: list[Tool] = []


def _ensure_unique_tool_names(tools: Sequence[Tool]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            duplicates.add(tool.name)
        seen.add(tool.name)
    if duplicates:
        msg = f"Agent tools must have unique names: {sorted(duplicates)}"
        raise ValueError(msg)


def register_agent_tools(*tools: Tool) -> None:
    """Register tools for every scan agent built afterwards（对齐 strix）。"""
    new_tools: list[Tool] = []
    for tool in tools:
        if tool not in _EXTRA_TOOLS and tool not in new_tools:
            new_tools.append(tool)

    _ensure_unique_tool_names([*_BASE_TOOLS, *_EXTRA_TOOLS, *new_tools, finish_scan, agent_finish])

    for tool in new_tools:
        _EXTRA_TOOLS.append(tool)
        logger.info("Registered extra agent tool: %s", getattr(tool, "name", tool))


def registered_agent_tools() -> tuple[Tool, ...]:
    """Return the currently registered scan-agent tools."""
    return tuple(_EXTRA_TOOLS)


# ---------------------------------------------------------------------------
# Agent 构建
# ---------------------------------------------------------------------------


def _make_shell_tools(
    sandbox_session: Any,
    allowed_targets: tuple[str, ...],
    *,
    strict_schemas: bool = True,
) -> list[Tool]:
    """从 SDK Shell capability 生成绑定到 live session 的 shell 工具。

    对齐 strix：capability.bind(session) 后 tools() 生成 exec_command +
    write_stdin（session 支持 PTY 时），再经 configure_tools 包装。
    """
    shell = Shell(
        configure_tools=lambda toolset: _configure_shell_tools(
            toolset, allowed_targets=allowed_targets, strict_schemas=strict_schemas
        )
    )
    shell.bind(sandbox_session)
    return shell.tools()


def _make_filesystem_tools(
    sandbox_session: Any,
    *,
    strict_schemas: bool = True,
    supports_images: bool = True,
) -> list[Tool]:
    """从 SDK Filesystem capability 生成绑定到 live session 的文件系统工具。

    对齐 strix：capability.bind(session) 后 tools() 生成 view_image +
    apply_patch（chat_completions 路由下转成 FunctionTool，输入字段名
    ``patch``）；模型不支持视觉时 view_image 挂载但禁用。
    """
    filesystem = Filesystem(
        configure_tools=lambda toolset: _configure_filesystem_tools(
            toolset, strict_schemas=strict_schemas, supports_images=supports_images
        )
    )
    filesystem.bind(sandbox_session)
    return filesystem.tools()


def build_agent(
    *,
    name: str = "agent",
    skills: list[str] | None = None,
    is_root: bool,
    sandbox_session: Any,
    allowed_targets: tuple[str, ...] = (),
    scan_mode: str = "blackbox",
    is_whitebox: bool = False,
    is_diff_scoped: bool = False,
    interactive: bool = False,
    parallel_tool_calls: bool = False,
    strict_tool_schemas: bool = True,
    system_prompt_context: dict[str, Any] | None = None,
    instructions_override: str | None = None,
    supports_images: bool = False,
    extra_tools: Sequence[Tool] | None = None,
    finish_tool: Tool = finish_scan,
    coverage_tools: bool = True,
    prompts_dir: Path | None = None,
    model_name: Any = None,
) -> Agent[Any]:
    """Build a Ben Agent for either root or child use（对齐 strix build_strix_agent）。

    Args:
        sandbox_session: SDK SandboxSession；Shell capability 绑定它生成
            exec_command / write_stdin（Ben 手动 bind，见模块 docstring）。
        allowed_targets: 授权白名单，exec_command invoke 前逐主机校验。
        strict_tool_schemas: 以 strict-schema 发送函数工具；不支持的路由关闭。
        instructions_override: 原样用作系统提示词，替代内建渲染。
        extra_tools: 仅本 agent 的追加工具（在 register_agent_tools 之上）。
        finish_tool: root agent 结束运行的工具（默认 finish_scan）。
        coverage_tools: 是否挂载覆盖台账工具及其提示词规则。
        model=None: 模型解析推迟到 RunConfig。
        model_name: 该 agent 的模型覆盖（模型分层）。None 表示沿用 run 级
            RunConfig.model；传 ``厂商/模型`` 名或 BenProvider 解析出的 Model
            实例（build_flash_model 的产物）时，SDK 用它替代 run 级模型——
            这是 root 旗舰档 / 子 agent flash 档的实现机制。
    """
    if instructions_override is not None:
        instructions = instructions_override
    else:
        instructions = render_system_prompt(
            requested_skills=skills,
            scan_mode=scan_mode,
            is_root=is_root,
            is_whitebox=is_whitebox,
            is_diff_scoped=is_diff_scoped,
            interactive=interactive,
            parallel_tool_calls=parallel_tool_calls,
            system_prompt_context=system_prompt_context,
            supports_images=supports_images,
            report_tool=True,
            coverage_tools=coverage_tools,
            prompts_dir=prompts_dir,
        )

    agent_tools = [*_EXTRA_TOOLS, *(extra_tools or [])]
    if interactive:
        # 只有附着了用户时，交还控制权才有意义（对齐 strix）。
        agent_tools.append(wait_for_user)
    base_tools = list(_BASE_TOOLS)
    if not coverage_tools:
        base_tools = [t for t in base_tools if t not in _COVERAGE_TOOLS]
        if finish_tool is finish_scan:
            finish_tool = _without_coverage_step(finish_scan)
    if is_root:
        tools: list[Tool] = [*base_tools, *agent_tools, finish_tool]
    else:
        tools = [*base_tools, *agent_tools, agent_finish]
    # shell / filesystem 工具由 SDK capability 绑定 session 生成（Ben 手动
    # bind），置于最前
    tools = [
        *_make_shell_tools(sandbox_session, allowed_targets, strict_schemas=strict_tool_schemas),
        *_make_filesystem_tools(
            sandbox_session,
            strict_schemas=strict_tool_schemas,
            supports_images=supports_images,
        ),
        *tools,
    ]
    _ensure_unique_tool_names(tools)
    tools = [
        _with_bounded_result(_with_strictness(_with_coerced_arguments(tool), strict_tool_schemas))
        if isinstance(tool, FunctionTool)
        else tool
        for tool in tools
    ]

    logger.info(
        "Built %s agent '%s' (skills=%d, tools=%d, scan_mode=%s, whitebox=%s)",
        "root" if is_root else "child",
        name,
        len(skills or []),
        len(tools),
        scan_mode,
        is_whitebox,
    )

    return Agent(
        name=name,
        instructions=instructions,
        tools=tools,
        tool_use_behavior=_finish_tool_use_behavior,
        model=model_name,
    )


def make_child_factory(
    *,
    sandbox_session: Any,
    allowed_targets: tuple[str, ...] = (),
    scan_mode: str = "blackbox",
    is_whitebox: bool = False,
    is_diff_scoped: bool = False,
    interactive: bool = False,
    parallel_tool_calls: bool = False,
    strict_tool_schemas: bool = True,
    system_prompt_context: dict[str, Any] | None = None,
    supports_images: bool = False,
    coverage_tools: bool = True,
    prompts_dir: Path | None = None,
    model_name: Any = None,
) -> Any:
    """Return the runner-owned builder used by ``spawn_child_agent``（对齐 strix）。

    运行级参数（``scan_mode``、``sandbox_session``、``allowed_targets`` 等）
    由闭包捕获，每个 child 继承扫描级配置，graph 工具无需感知 runner 内部。
    ``model_name`` 即模型分层的子 agent 档位：runner 传入 build_flash_model
    的产物（未配置 flash 档时为 None，子 agent 沿用 run 级旗舰模型）。
    """

    def _factory(*, name: str, skills: list[str]) -> Agent[Any]:
        return build_agent(
            name=name,
            skills=skills,
            is_root=False,
            sandbox_session=sandbox_session,
            allowed_targets=allowed_targets,
            scan_mode=scan_mode,
            is_whitebox=is_whitebox,
            is_diff_scoped=is_diff_scoped,
            interactive=interactive,
            parallel_tool_calls=parallel_tool_calls,
            strict_tool_schemas=strict_tool_schemas,
            system_prompt_context=system_prompt_context,
            supports_images=supports_images,
            coverage_tools=coverage_tools,
            prompts_dir=prompts_dir,
            model_name=model_name,
        )

    return _factory
