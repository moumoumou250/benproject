"""MCP 发现与调度 @function_tool（对齐 strix 的 mcp/agent_tools.py 模式）。

把旧的 ToolSpec 版 list_mcps / call_mcp 转为 SDK 的 @function_tool 装饰函数。
"""

from __future__ import annotations

import json
from typing import Any

from agents import function_tool
from agents.run_context import RunContextWrapper

from Ben.tools.mcp import client
from Ben.tools.mcp.loader import load_mcp_configs


@function_tool
def list_mcps(ctx: RunContextWrapper[Any]) -> str:
    """列出本次可用的 MCP 连接及其工具清单。

    返回每个连接的工具名、说明与参数 input schema。
    调用外部 MCP 工具前先使用本工具发现可用连接；无配置时返回空列表。
    """
    configs = load_mcp_configs()
    connections = client.list_connections(configs)

    if not connections:
        return json.dumps(
            {
                "ok": True,
                "connections": [],
                "message": "本次未配置任何 MCP 连接。",
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {"ok": True, "connections": connections},
        ensure_ascii=False,
        default=str,
    )


@function_tool(strict_mode=False)
def call_mcp(
    ctx: RunContextWrapper[Any],
    connection: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """调用指定 MCP 连接上的一个工具并返回真实结果。

    先用 list_mcps 确认连接名与工具名，再按该工具的 input schema 传 arguments。

    Args:
        connection: MCP 连接名，与 list_mcps 返回的 name 一致
        tool: 目标连接上的工具名，与 list_mcps 返回的工具名一致
        arguments: 工具参数，键值对形式；无参数工具可省略
    """
    tool_arguments = arguments or {}
    configs = load_mcp_configs()

    try:
        payload = client.call_tool(
            configs, connection.strip(), tool.strip(), tool_arguments
        )
    except ValueError as exc:
        return json.dumps(
            {"ok": False, "connection": connection, "tool": tool, "error": str(exc)},
            ensure_ascii=False,
        )

    if payload["is_error"]:
        return json.dumps(
            {
                "ok": False,
                "error": f"MCP 服务器返回工具错误：{payload['text'] or '(无错误文本)'}",
                "data": payload,
            },
            ensure_ascii=False,
            default=str,
        )

    text = payload["text"] or "(工具未返回文本内容)"
    if payload["structured"]:
        text += f"\n[另有 {len(payload['structured'])} 项非文本结构化内容]"
    return json.dumps(
        {
            "ok": True,
            "summary": f"连接 {connection} 的工具 {tool} 调用成功：\n{text}",
            "data": payload,
        },
        ensure_ascii=False,
        default=str,
    )
