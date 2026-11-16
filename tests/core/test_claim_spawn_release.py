"""H7 / H9 / M8：claim 释放与 create_agent 校验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agents.tool_context import ToolContext

from Ben.core.agents import AgentCoordinator
from Ben.core.execution import spawn_child_agent
from Ben.core.task_claims import (
    hydrate_task_claims_from_disk,
    owner_of,
    reset_task_claims,
    try_claim,
)
from Ben.tools.agents_graph.tools import create_agent


@pytest.fixture(autouse=True)
def _claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEN_CLAIM", "1")
    reset_task_claims()
    hydrate_task_claims_from_disk(tmp_path)
    yield
    reset_task_claims()


@pytest.mark.asyncio
async def test_claim_released_when_spawn_fails(tmp_path: Path) -> None:
    coord = AgentCoordinator()
    await coord.register("root", "Root", parent_id=None)

    def _bad_factory(**kwargs: Any) -> Any:
        raise RuntimeError("factory boom")

    with pytest.raises(RuntimeError, match="factory boom"):
        await spawn_child_agent(
            coordinator=coord,
            factory=_bad_factory,
            agents_db_path=tmp_path / "agents.db",
            sessions_to_close=[],
            run_config=AsyncMock(),
            max_turns=5,
            interactive=False,
            parent_ctx={"agent_id": "root"},
            name="Hunter",
            task="test",
            skills=[],
            parent_history=[],
            surface="/api/x",
            risk_area="xss",
            tier="hunter",
        )
    assert owner_of("/api/x", "xss") is None


@pytest.mark.asyncio
async def test_create_agent_requires_surface_when_claims_on() -> None:
    coord = AgentCoordinator()
    await coord.register("root", "Root", parent_id=None)
    spawner = AsyncMock(return_value={"success": True, "agent_id": "c1"})

    async def _call(args: dict[str, Any]) -> dict[str, Any]:
        ctx = ToolContext(
            context={
                "coordinator": coord,
                "agent_id": "root",
                "spawn_child_agent": spawner,
            },
            tool_name=create_agent.name,
            tool_call_id="c1",
            tool_arguments="{}",
        )
        raw: str = await create_agent.on_invoke_tool(ctx, json.dumps(args))
        return cast("dict[str, Any]", json.loads(raw))

    denied = await _call(
        {"name": "H", "task": "t", "tier": "hunter"}
    )
    assert denied["success"] is False
    assert "surface" in denied["error"].lower()
    spawner.assert_not_awaited()

    ok_validator = await _call(
        {"name": "V", "task": "t", "tier": "validator"}
    )
    assert ok_validator["success"] is True
    spawner.assert_awaited()


@pytest.mark.asyncio
async def test_general_tier_factory_error_is_not_swallowed(tmp_path: Path) -> None:
    coord = AgentCoordinator()
    await coord.register("root", "Root", parent_id=None)

    def _factory(
        name: str,
        skills: list | None = None,
        model_name: str | None = None,
        use_root_capabilities: bool = False,
    ) -> Any:
        raise TypeError("unexpected kw inside factory")

    with pytest.raises(TypeError, match="unexpected kw"):
        await spawn_child_agent(
            coordinator=coord,
            factory=_factory,
            agents_db_path=tmp_path / "agents.db",
            sessions_to_close=[],
            run_config=AsyncMock(),
            max_turns=5,
            interactive=False,
            parent_ctx={"agent_id": "root"},
            name="G",
            task="t",
            skills=[],
            parent_history=[],
            surface="/a",
            risk_area="auth",
            tier="general",
        )
