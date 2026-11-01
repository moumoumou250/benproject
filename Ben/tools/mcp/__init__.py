"""MCP（Model Context Protocol）客户端模块。

让 Ben 的 agent 能连接外部 MCP 服务器，按需发现并调用其工具。对外暴露配置
加载与客户端核心函数；面向模型的 list_mcps / call_mcp 工具以 @function_tool
形式定义于 ``Ben.tools.mcp.agent_tools``（对齐 strix 的 tools/mcp/agent_tools.py）。
"""

from Ben.tools.mcp.client import call_tool, list_connections
from Ben.tools.mcp.config import McpConnectionConfig
from Ben.tools.mcp.loader import load_mcp_configs

__all__ = [
    "McpConnectionConfig",
    "load_mcp_configs",
    "list_connections",
    "call_tool",
]
