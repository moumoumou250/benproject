"""@function_tool 工具函数定义（对齐 strix 的 tools 模式）。

每个工具是 ``@function_tool`` 装饰的函数，第一个参数为 SDK 的
``RunContextWrapper``，工具从 ``ctx.context`` 中取出运行时依赖
（sandbox_session、allowed_targets、command_timeout 等）。

这与 strix 的 ``@function_tool`` 工具模式一致：
- strix/tools/finish/tool.py -> finish_scan(ctx, ...)
- strix/tools/mcp/agent_tools.py -> list_mcps(ctx, ...)
- ...

Ben 的工具上下文（ctx.context）是一个 dict，由 runner 注入：
    {
        "sandbox_session": SandboxSession,  # SDK 的 sandbox 会话
        "allowed_targets": tuple[str, ...],
        "command_timeout": float,
        "target": str,
    }
"""

from __future__ import annotations

from agents import function_tool
from agents.run_context import RunContextWrapper

from Ben.tools.function_tools.http_check import http_check
from Ben.tools.function_tools.load_skill import load_skill
from Ben.tools.function_tools.mcp_tools import call_mcp, list_mcps

__all__ = [
    "http_check",
    "load_skill",
    "list_mcps",
    "call_mcp",
]
