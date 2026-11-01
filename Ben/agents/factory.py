"""Build the Ben Agent with openai-agents SDK（对齐 strix 的 factory.py）。

使用普通 ``Agent`` 而非 ``SandboxAgent``，因为 DeepSeek/GLM 端点只支持
chat_completions API，而 SandboxAgent 的 Filesystem/Shell capabilities
会注册 hosted tools（apply_patch/view_image/exec_command），这些在
chat_completions 模式下不被支持。

Ben 的 http_check 工具通过 ctx.context 中的 sandbox_session 直接调用
SDK sandbox 的 exec 方法执行命令，不需要 Filesystem/Shell capabilities。

model=None 把模型解析推迟到 RunConfig。

运行验证：
    .venv/Scripts/python.exe -c "from Ben.agents.factory import build_agent; a=build_agent(); print(a.name, [t.name for t in a.tools])"
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from agents import Agent
from agents.tool import Tool

from Ben.tools.function_tools import call_mcp, http_check, list_mcps, load_skill

logger = logging.getLogger(__name__)

# Ben 的基础工具集（@function_tool 装饰的函数对象）
_BASE_TOOLS: tuple[Tool, ...] = (
    http_check,
    load_skill,
    list_mcps,
    call_mcp,
)


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


def build_agent(
    *,
    name: str = "ben-agent",
    instructions: str = "You are a security testing agent. Use tools to investigate authorized targets.",
    tools: Sequence[Tool] = _BASE_TOOLS,
) -> Agent[Any]:
    """Return the Ben Agent: mounted @function_tool tools.

    - ``tools``: Ben 的 @function_tool 工具集（http_check, load_skill, list_mcps, call_mcp）
    - ``model=None``: 模型解析推迟到 RunConfig
    - 不使用 SandboxAgent/capabilities（chat_completions API 不支持 hosted tools）
    """
    _ensure_unique_tool_names(tools)
    logger.info("Built agent '%s' (tools=%d)", name, len(tools))
    return Agent(
        name=name,
        instructions=instructions,
        tools=list(tools),
        model=None,
    )


if __name__ == "__main__":
    agent = build_agent()
    print(f"agent: {agent.name}")
    print(f"tools: {[tool.name for tool in agent.tools]}")
