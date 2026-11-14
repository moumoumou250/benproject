"""通用 MCP 客户端：连接 MCP 服务器并按需触达它们的工具。

对外暴露逐运行 registry、受监督会话、连接配置加载，以及面向模型的五个
发现/派发工具（``list_mcps`` / ``describe_mcp`` / ``search_mcp_tools`` /
``get_mcp_tool_schema`` / ``call_mcp``，定义于
``Ben.tools.mcp.agent_tools``，对齐 strix 的 tools/mcp 包导出名单）。
"""

from __future__ import annotations

from Ben.tools.mcp.agent_tools import (
    call_mcp,
    describe_mcp,
    get_mcp_tool_schema,
    list_mcps,
    search_mcp_tools,
)
from Ben.tools.mcp.client import (
    ConnectedMcpServer,
    attach_mcp_requests,
    connect_mcp_servers,
)
from Ben.tools.mcp.config import McpConnectionConfig
from Ben.tools.mcp.failures import FailureInfo, HttpStatusRecorder, classify
from Ben.tools.mcp.loader import load_mcp_configs
from Ben.tools.mcp.naming import namespaced_tool_name
from Ben.tools.mcp.registry import (
    CALL_MCP_TOOL,
    DESCRIBE_MCP_TOOL,
    GET_MCP_TOOL_SCHEMA_TOOL,
    MCP_DISPATCH_TOOLS,
    MCP_REGISTRY_CONTEXT_KEY,
    SEARCH_MCP_TOOLS_TOOL,
    McpCallInfo,
    McpConnectionEntry,
    McpConnectionRequest,
    McpConnectionStatus,
    McpConnectionSummary,
    McpRegistry,
    resolve_mcp_call,
)
from Ben.tools.mcp.session import McpConnectionUnavailableError, SupervisedMcpSession


__all__ = [
    "CALL_MCP_TOOL",
    "DESCRIBE_MCP_TOOL",
    "GET_MCP_TOOL_SCHEMA_TOOL",
    "MCP_DISPATCH_TOOLS",
    "MCP_REGISTRY_CONTEXT_KEY",
    "SEARCH_MCP_TOOLS_TOOL",
    "ConnectedMcpServer",
    "FailureInfo",
    "HttpStatusRecorder",
    "McpCallInfo",
    "McpConnectionConfig",
    "McpConnectionEntry",
    "McpConnectionRequest",
    "McpConnectionStatus",
    "McpConnectionSummary",
    "McpConnectionUnavailableError",
    "McpRegistry",
    "SupervisedMcpSession",
    "attach_mcp_requests",
    "call_mcp",
    "classify",
    "connect_mcp_servers",
    "describe_mcp",
    "get_mcp_tool_schema",
    "list_mcps",
    "load_mcp_configs",
    "namespaced_tool_name",
    "resolve_mcp_call",
    "search_mcp_tools",
]
