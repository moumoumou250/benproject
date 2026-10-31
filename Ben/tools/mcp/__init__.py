"""MCP（Model Context Protocol）客户端模块。

让 Ben 的 agent 能连接外部 MCP 服务器，按需发现并调用其工具。对外暴露两个
已适配 ToolRegistry 的工具规格，以及配置加载与客户端核心函数。
"""

from Ben.tools.mcp.client import call_tool, list_connections
from Ben.tools.mcp.config import McpConnectionConfig
from Ben.tools.mcp.loader import load_mcp_configs
from Ben.tools.mcp.tools import CALL_MCP_SPEC, LIST_MCP_SPEC

__all__ = [
    "McpConnectionConfig",
    "load_mcp_configs",
    "list_connections",
    "call_tool",
    "LIST_MCP_SPEC",
    "CALL_MCP_SPEC",
]
