"""Build the simplest SandboxAgent.

Modeled on the ``SandboxAgent(...)`` construction at the end of
``strix/agents/factory.py`` (``build_strix_agent``), stripped to the
essentials:

    name + instructions + tools + capabilities, with model=None

- ``tools`` are SDK function tools the model calls directly; names must
  be unique (mirrors ``_ensure_unique_tool_names`` in the strix factory)
- ``capabilities`` mount the sandbox-native tools: Filesystem exposes
  view_image / apply_patch, Shell exposes exec_command / write_stdin.
  Their tool objects are created lazily, only once the agent is bound
  to a sandbox session at run time, so construction here never touches
  Docker
- ``model=None`` defers model resolution to the Runner's RunConfig

Run with the project venv for a build smoke check:

    .venv/Scripts/python.exe Ben/agents/factory.py
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from agents import function_tool
from agents.sandbox import SandboxAgent
from agents.sandbox.capabilities import Filesystem, Shell
from agents.tool import Tool

logger = logging.getLogger(__name__)

@function_tool
def echo(text: str) -> str:
    """Echo the input text back unchanged."""
    return text

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

def build_agent(
    *,
    name: str = "agent",
    instructions: str = "You are a helpful sandbox agent.",
    tools: Sequence[Tool] = (echo,),
) -> SandboxAgent[Any]:
    """Return the simplest SandboxAgent: mounted tools + sandbox capabilities."""
    _ensure_unique_tool_names(tools)
    logger.info("Built agent '%s' (tools=%d)", name, len(tools))
    return SandboxAgent(
        name=name,
        instructions=instructions,
        tools=list(tools),
        model=None,
        capabilities=[Filesystem(), Shell()],
    )

if __name__ == "__main__":
    agent = build_agent()
    print(f"agent: {agent.name}")
    print(f"tools: {[tool.name for tool in agent.tools]}")
    print(f"capabilities: {[type(c).__name__ for c in agent.capabilities]}")