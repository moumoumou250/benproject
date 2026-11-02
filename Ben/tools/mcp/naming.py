"""MCP 服务器的工具如何对模型命名（对齐 strix 的 mcp/naming.py）。

与客户端分离、仅依赖标准库，使调用方无需导入 MCP 客户端（以及它连带
的 agents SDK 与全部已注册工具）即可构造某连接下工具的模型侧名称。
"""

from __future__ import annotations

import re


# 提供给模型的工具名只能是字母、数字、下划线或连字符，其他字符会被模型 API
# 直接拒绝。三类来源可能把杂字符混进名字：连接名与工具名之间的分隔符、服务
# 器自己起的工具名（服务器常自带命名空间）、用户配置文件里的连接名。对拼好
# 的完整名称统一清洗即可一并覆盖三者。
_INVALID_TOOL_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def namespaced_tool_name(connection: str, tool: str) -> str:
    """连接下某工具对模型暴露的名称。

    只有模型侧名称被改写。对服务器的每次调用都使用服务器自报的工具名，
    因此这里的清洗永远不会改变实际调用的是哪个工具。
    """
    return _INVALID_TOOL_NAME_CHARS.sub("_", f"{connection}_{tool}")
