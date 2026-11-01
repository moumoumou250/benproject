"""Build the Ben Agent with openai-agents SDK（对齐 strix 的 factory.py）。

Shell 工具直接来自 SDK 的 ``Shell`` capability（exec_command + write_stdin），
per-run 绑定到 live sandbox session——与 strix 同一套实现，不再使用
Ben 手写的 exec_command @function_tool。PTY 会话、流式输出、token 截断、
交互式进程（tty=true + write_stdin）全部由 SDK 提供。

由于 DeepSeek/GLM 端点只支持 chat_completions API，Agent 仍用普通 ``Agent``
而非 ``SandboxAgent``（后者的 capability 装配路径会引入 hosted tools）。
这里手动 ``Shell.bind(session)`` 后取出生成的 FunctionTool 挂载，绕开
hosted tools 限制。

对齐的 strix 包装（strix/agents/factory.py）：
- ``_wrap_exec_command``       默认 shell=bash、max_output_tokens 上限、
  参数校验错误格式化、非 TTY 长跑进程追加提示
- ``_wrap_write_stdin``        chars 转义解码、max_output_tokens 上限
- ``_function_tool_with_error_result``  工具异常一律作为结果返回给模型

Ben 特有硬约束：exec_command invoke 前做授权白名单校验——命令中提取的
每个主机名/IP 都必须在授权范围内，否则拒绝执行。

model=None 把模型解析推迟到 RunConfig。

运行验证（需 Docker 运行中）：
    .venv/Scripts/python.exe -c "
    import asyncio, docker
    from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
    from Ben.agents.factory import build_agent
    async def main():
        s = await DockerSandboxClient(docker.from_env()).create(options=DockerSandboxClientOptions(image='ben-executor'))
        a = build_agent(sandbox_session=s, allowed_targets=('http://127.0.0.1',))
        print(a.name, [t.name for t in a.tools])
        await s.aclose()
    asyncio.run(main())
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from agents import Agent
from agents.sandbox.capabilities import Shell
from agents.sandbox.session.pty_types import PTY_YIELD_TIME_MS_MAX
from agents.tool import FunctionTool, Tool
from pydantic import ValidationError

from Ben.tools.load_skill.tool import load_skill
from Ben.tools.mcp.agent_tools import call_mcp, list_mcps
from Ben.tools.proxy.tools import (
    list_requests,
    list_sitemap,
    repeat_request,
    scope_rules,
    view_request,
    view_sitemap_entry,
)

logger = logging.getLogger(__name__)

# Ben 的本地工具集（@function_tool 装饰的函数对象）；shell 工具由 SDK capability 生成
_LOCAL_TOOLS: tuple[Tool, ...] = (
    load_skill,
    list_mcps,
    call_mcp,
    list_requests,
    view_request,
    repeat_request,
    list_sitemap,
    view_sitemap_entry,
    scope_rules,
)

# max_output_tokens 上限（对齐 strix _apply_shell_output_cap 的 ceiling 语义）
_MAX_OUTPUT_TOKENS_CEILING = 25_000


# ---------------------------------------------------------------------------
# 授权白名单（Ben 特有硬约束，原 Ben/tools/exec_command 的校验逻辑）
# ---------------------------------------------------------------------------

# 回环/本机地址视为沙箱内部操作，不做白名单校验
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

# 从命令字符串中提取网络目标的正则
_URL_HOST_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://([A-Za-z0-9_.\-]+)(?::\d+)?")
_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$"
)
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _extract_hosts(command: str) -> set[str]:
    """从命令字符串中提取全部网络目标（URL 主机、域名、IPv4）。

    裸主机名（无点号的 docker 内部服务名，如 juice-shop）不匹配域名规则，
    不会被误判为外网目标；版本号（1.2.3）等数字串不会匹配 IPv4。
    """
    hosts: set[str] = set()
    for match in _URL_HOST_RE.finditer(command):
        hosts.add(match.group(1).lower())

    for raw_token in re.split(r"\s+", command):
        token = raw_token.strip("\"'`;|&()<>")
        if "=" in token:  # key=value 形式只检查 value 部分
            token = token.split("=", 1)[1].strip("\"'")
        if not token or "/" in token:  # 路径（含 /）不是主机
            continue
        if token.count(":") == 1:  # host:port 形式
            maybe_host, _, port = token.partition(":")
            if port.isdigit():
                token = maybe_host
        if _IPV4_RE.match(token) or _DOMAIN_RE.match(token):
            hosts.add(token.lower())
    return hosts


def _allowed_hostnames(allowed_targets: tuple[str, ...]) -> set[str]:
    """白名单配置 -> 主机名集合（小写）。"""
    from urllib.parse import urlsplit

    hosts: set[str] = set()
    for target in allowed_targets:
        try:
            hostname = urlsplit(target).hostname
        except ValueError:
            continue
        if hostname:
            hosts.add(hostname.lower())
    return hosts


def _check_command_allowed(command: str, allowed_targets: tuple[str, ...]) -> set[str]:
    """返回命令中未授权的主机集合（空集合 = 通过）。"""
    hosts = _extract_hosts(command) - _LOCAL_HOSTS
    if not hosts:
        return set()
    allowed = _allowed_hostnames(allowed_targets)
    return {h for h in hosts if h not in allowed}


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
    requested = parsed.get("max_output_tokens")
    parsed["max_output_tokens"] = (
        _MAX_OUTPUT_TOKENS_CEILING
        if not isinstance(requested, int) or requested > _MAX_OUTPUT_TOKENS_CEILING
        else requested
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
    return f"{result[: header.end()]}{notice}\n{result[header.end():]}"


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
                unauthorized = _check_command_allowed(cmd, allowed_targets)
                if unauthorized:
                    return json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"命令包含未授权目标 {sorted(unauthorized)}，已拒绝执行。"
                                f"授权范围：{sorted(_allowed_hostnames(allowed_targets))}"
                            ),
                        },
                        ensure_ascii=False,
                    )
            if "shell" not in parsed:
                parsed["shell"] = "bash"
            _apply_shell_output_cap(parsed)
            raw_input = json.dumps(parsed)
        try:
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


def _function_tool_with_error_result(tool: FunctionTool) -> FunctionTool:
    """工具异常不抛出，一律作为 JSON 错误结果返回给模型（chat_completions 模式）。"""
    invoke_tool = tool.on_invoke_tool

    async def invoke(ctx: Any, raw_input: str) -> Any:
        try:
            return await invoke_tool(ctx, raw_input)
        except Exception as exc:  # noqa: BLE001 - 工具错误应对模型可见
            logger.debug("Tool %s failed; returning error as result", tool.name, exc_info=True)
            return json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )

    tool.on_invoke_tool = invoke
    return tool


def _configure_shell_tools(toolset: Any, *, allowed_targets: tuple[str, ...]) -> None:
    """对齐 strix _configure_shell_tools：逐个包装 SDK 生成的 shell 工具。"""
    for name, tool in vars(toolset).items():
        if not isinstance(tool, FunctionTool):
            continue
        if tool.name == "exec_command":
            tool = _wrap_exec_command(tool, allowed_targets=allowed_targets)
        elif tool.name == "write_stdin":
            tool = _wrap_write_stdin(tool)
        tool = _function_tool_with_error_result(tool)
        setattr(toolset, name, tool)


# ---------------------------------------------------------------------------
# Agent 构建
# ---------------------------------------------------------------------------


def _ensure_unique_tool_names(tools: Sequence[Tool]) -> None:
    """确保工具名唯一（对齐 strix 的 _ensure_unique_tool_names）。"""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            duplicates.add(tool.name)
        seen.add(tool.name)
    if duplicates:
        msg = f"Agent tools must have unique names: {sorted(duplicates)}"
        raise ValueError(msg)


def _make_shell_tools(
    sandbox_session: Any, allowed_targets: tuple[str, ...]
) -> list[Tool]:
    """从 SDK Shell capability 生成绑定到 live session 的 shell 工具。

    对齐 strix：capability.bind(session) 后 tools() 生成 exec_command +
    write_stdin（session 支持 PTY 时），再经 configure_tools 包装。
    """
    shell = Shell(
        configure_tools=lambda toolset: _configure_shell_tools(
            toolset, allowed_targets=allowed_targets
        )
    )
    shell.bind(sandbox_session)
    return shell.tools()


def build_agent(
    *,
    name: str = "ben-agent",
    instructions: str = "You are a security testing agent. Use tools to investigate authorized targets.",
    sandbox_session: Any,
    allowed_targets: tuple[str, ...] = (),
    extra_tools: Sequence[Tool] = (),
) -> Agent[Any]:
    """Return the Ben Agent: SDK shell 工具（绑定 session）+ Ben 本地工具。

    - ``sandbox_session``: SDK 的 SandboxSession；Shell capability 绑定它生成
      exec_command / write_stdin（与 strix 同一实现）
    - ``allowed_targets``: 授权白名单，exec_command invoke 前逐主机校验
    - ``extra_tools``: 追加工具（默认挂载 load_skill/list_mcps/call_mcp）
    - ``model=None``: 模型解析推迟到 RunConfig
    - 不使用 SandboxAgent/capabilities 装配路径（chat_completions API 不支持
      hosted tools），这里手动 bind 后取 FunctionTool
    """
    tools: list[Tool] = [
        *_make_shell_tools(sandbox_session, allowed_targets),
        *_LOCAL_TOOLS,
        *extra_tools,
    ]
    _ensure_unique_tool_names(tools)
    logger.info("Built agent '%s' (tools=%d)", name, len(tools))
    return Agent(
        name=name,
        instructions=instructions,
        tools=tools,
        model=None,
    )
