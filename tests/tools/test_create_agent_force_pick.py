"""create_agent 强制 DFS 代领 recon/enum intent。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agents.tool_context import ToolContext

from Ben.core.agents import AgentCoordinator
from Ben.core.task_graph import add_fact, hydrate_task_graph_from_disk, reset_task_graph
from Ben.tools.agents_graph.tools import create_agent


def setup_function() -> None:
    reset_task_graph()


@pytest.mark.asyncio
async def test_recon_without_intent_picks_dfs_head(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    fact = add_fact(kind="host", title="target", data={"host": "t.example"})
    intent_id = fact["intent_id"]
    assert intent_id

    coordinator = AgentCoordinator()
    await coordinator.register("root", "Root", parent_id=None)

    spawned: dict[str, Any] = {}

    async def spawner(**kwargs: Any) -> dict[str, Any]:
        spawned.update(kwargs)
        return {
            "success": True,
            "agent_id": "abc12345",
            "name": kwargs.get("name"),
            "intent_id": kwargs.get("intent_id"),
            "profile": kwargs.get("profile"),
        }

    ctx = ToolContext(
        context={
            "coordinator": coordinator,
            "agent_id": "root",
            "spawn_child_agent": spawner,
        },
        tool_name=create_agent.name,
        tool_call_id="c1",
        tool_arguments="{}",
    )
    raw = await create_agent.on_invoke_tool(
        ctx,
        json.dumps(
            {
                "name": "Recon Worker",
                "task": "",
                "profile": "recon",
                "tier": "scout",
            }
        ),
    )
    result = cast("dict[str, Any]", json.loads(raw))
    assert result["success"] is True
    assert spawned.get("intent_id") == intent_id
    assert spawned.get("profile") == "recon"


@pytest.mark.asyncio
async def test_recon_without_intents_errors(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    coordinator = AgentCoordinator()
    await coordinator.register("root", "Root", parent_id=None)

    ctx = ToolContext(
        context={
            "coordinator": coordinator,
            "agent_id": "root",
            "spawn_child_agent": AsyncMock(),
        },
        tool_name=create_agent.name,
        tool_call_id="c2",
        tool_arguments="{}",
    )
    raw = await create_agent.on_invoke_tool(
        ctx,
        json.dumps(
            {
                "name": "Recon Worker",
                "task": "scan something",
                "profile": "recon",
                "tier": "scout",
            }
        ),
    )
    result = cast("dict[str, Any]", json.loads(raw))
    assert result["success"] is False
    assert "no open recon" in result["error"]
