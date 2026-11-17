"""recon/enum profile 的结构化 finish 契约。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from agents.tool_context import ToolContext

from Ben.core.agents import AgentCoordinator
from Ben.core.task_graph import reset_task_graph
from Ben.tools.agents_graph.tools import agent_finish


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_task_graph()


async def _call(
    coordinator: AgentCoordinator,
    args: dict[str, Any],
    *,
    profile: str = "recon",
) -> dict[str, Any]:
    ctx = ToolContext(
        context={
            "coordinator": coordinator,
            "agent_id": "child",
            "parent_id": "root",
            "task": "t",
            "agent_profile": profile,
        },
        tool_name=agent_finish.name,
        tool_call_id="call-1",
        tool_arguments="{}",
    )
    raw: str = await agent_finish.on_invoke_tool(ctx, json.dumps(args))
    return cast("dict[str, Any]", json.loads(raw))


@pytest.mark.asyncio
async def test_recon_finish_refused_without_structured_exit() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "Root", parent_id=None)
    await coordinator.register("child", "Child", parent_id="root")

    result = await _call(
        coordinator,
        {"success": True, "result_summary": "done nothing"},
    )
    assert result["success"] is False
    assert result.get("agent_completed") is False
    assert "structured exit" in result["error"]
    assert coordinator.statuses["child"] != "completed"


@pytest.mark.asyncio
async def test_recon_finish_accepts_result_json() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "Root", parent_id=None)
    await coordinator.register("child", "Child", parent_id="root")

    result = await _call(
        coordinator,
        {
            "success": True,
            "result_summary": "found ports",
            "result_json": json.dumps({"ports": [{"host": "h", "port": "80"}]}),
            "report_to_parent": False,
        },
    )
    assert result["success"] is True
    assert result.get("agent_completed") is True
    assert coordinator.statuses["child"] == "completed"
