"""H5：malformed ruled_out_json 不阻断 agent_finish。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from agents.tool_context import ToolContext

from Ben.core.agents import AgentCoordinator
from Ben.tools.agents_graph.tools import agent_finish


async def _call(coordinator: AgentCoordinator, args: dict[str, Any]) -> dict[str, Any]:
    ctx = ToolContext(
        context={
            "coordinator": coordinator,
            "agent_id": "child",
            "parent_id": "root",
            "task": "t",
        },
        tool_name=agent_finish.name,
        tool_call_id="call-1",
        tool_arguments="{}",
    )
    raw: str = await agent_finish.on_invoke_tool(ctx, json.dumps(args))
    return cast("dict[str, Any]", json.loads(raw))


@pytest.mark.asyncio
async def test_agent_finish_tolerates_malformed_ruled_out() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register("root", "Root", parent_id=None)
    await coordinator.register("child", "Child", parent_id="root")

    result = await _call(
        coordinator,
        {
            "success": True,
            "result_summary": "done",
            "ruled_out_json": "{not json",
        },
    )
    assert result["success"] is True
    assert coordinator.statuses["child"] == "completed"
