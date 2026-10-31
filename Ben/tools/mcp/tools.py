"""MCP 发现与调度工具（适配 Ben 的 ToolRegistry）。

两个 ToolSpec 组成最小闭环：
- list_mcps：查看本次配置了哪些 MCP 连接及其工具清单；
- call_mcp：调用指定连接上的一个工具，拿回真实结果。

不经过 Docker 执行器；handler 的 runtime 参数保留，仅为满足统一签名。
"""

from __future__ import annotations

from typing import Any

from Ben.tools.mcp import client
from Ben.tools.mcp.loader import load_mcp_configs
from Ben.tools.registry import (
    ToolResult,
    ToolRuntime,
    ToolValidationError,
    ToolSpec,
)


def _list_mcps(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    """加载配置并列出所有 MCP 连接的工具清单。"""
    configs = load_mcp_configs()
    connections = client.list_connections(configs)

    if not connections:
        return ToolResult(
            name="list_mcps",
            ok=True,
            summary=(
                "本次未配置任何 MCP 连接（默认配置文件 "
                "~/.ben/mcp-servers.json 不存在或为空）。"
            ),
            data={"connections": []},
        )

    lines: list[str] = []
    for conn in connections:
        header = f"连接 {conn['name']}（{conn['tool_count']} 个工具）"
        if conn.get("error"):
            header += f"：连接失败 - {conn['error']}"
        lines.append(header)
        if conn.get("notes"):
            lines.append(f"  说明：{conn['notes']}")
        for tool in conn["tools"]:
            desc = tool["description"] or "(无描述)"
            lines.append(f"  - {tool['name']}: {desc}")

    return ToolResult(
        name="list_mcps",
        ok=True,
        summary="\n".join(lines),
        data={"connections": connections},
    )


LIST_MCP_SPEC = ToolSpec(
    name="list_mcps",
    description=(
        "列出本次可用的 MCP 连接及其工具清单（工具名、说明、参数 input schema）。"
        "调用外部 MCP 工具前先使用本工具发现可用连接；无配置时返回空列表。"
    ),
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    handler=_list_mcps,
)

"""
若此时的arguments为
arguments = {
    "connection": "filesystem",
    "tool": "echo",
    "arguments": {
        "message": "hello world"
    }
}

"""
def _call_mcp(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    """
    从参数中提取MCP Server名称、工具名、工具参数
    connection = "filesystem"
    tool = "echo"
    tool_arguments = {"message": "hello world"}
    """
    connection = arguments.get("connection")
    tool = arguments.get("tool")
    tool_arguments = arguments.get("arguments", {}) or {}

    # 必须非空字符串
    if not isinstance(connection, str) or not connection.strip():
        raise ToolValidationError("connection 必须是非空字符串")
    if not isinstance(tool, str) or not tool.strip():
        raise ToolValidationError("tool 必须是非空字符串")
    if not isinstance(tool_arguments, dict):
        raise ToolValidationError("arguments 必须是 JSON 对象")

    # 获取 MCP 配置文件，获取 MCP Server 连接信息
    configs = load_mcp_configs()

    try:
        # 调用client.call_tool方法，调用MCP Server上的工具
        # 传入4参数， configs echo {"message": "hello world"}
        payload = client.call_tool(
            configs, connection.strip(), tool.strip(), tool_arguments
        )
    except ValueError as exc:
        # 连接/工具不存在、白名单拒绝等：作为失败结果回传给模型，不抛断循环
        return ToolResult(
            name="call_mcp",
            ok=False,
            summary="",
            data={"connection": connection, "tool": tool},
            error=str(exc),
        )

    if payload["is_error"]:
        return ToolResult(
            name="call_mcp",
            ok=False,
            summary="",
            data=payload,
            error=(
                "MCP 服务器返回工具错误："
                f"{payload['text'] or '(无错误文本)'}"
            ),
        )

    # 提取工具返回内容
    text = payload["text"] or "(工具未返回文本内容)"
    if payload["structured"]:
        text += (
            f"\n[另有 {len(payload['structured'])} 项非文本结构化内容，"
            "见 data.structured]"
        )
    return ToolResult(
        name="call_mcp",
        ok=True,
        summary=f"连接 {connection} 的工具 {tool} 调用成功：\n{text}",
        data=payload,
    )

# 此段被提前注册号，当模型调用call_mcp时，会调用此函数
# tool_result = self.registry.execute(tc.name, tc.arguments, self.runtime)
CALL_MCP_SPEC = ToolSpec(
    name="call_mcp",
    description=(
        "调用指定 MCP 连接上的一个工具并返回真实结果。先用 list_mcps 确认连接名"
        "与工具名，再按该工具的 input schema 传 arguments（无参数工具可省略）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "connection": {
                "type": "string",
                "description": "MCP 连接名，与 list_mcps 返回的 name 一致",
            },
            "tool": {
                "type": "string",
                "description": "目标连接上的工具名，与 list_mcps 返回的工具名一致",
            },
            "arguments": {
                "type": "object",
                "description": (
                    "工具参数，键值对形式，形状匹配该工具的 input_schema；"
                    "无参数工具可省略"
                ),
                "additionalProperties": True,
                "default": {},
            },
        },
        "required": ["connection", "tool"],
        "additionalProperties": False,
    },
    handler=_call_mcp,
)
