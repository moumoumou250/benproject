"""H6/H8/M6/M7：verify 归属、HTTP 字段、幂等、spawn 失败。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents.tool_context import ToolContext

from Ben.core.agents import AgentCoordinator
from Ben.tools.verify.tool import (
    _finding_looks_http,
    get_verify_result,
    hydrate_verify_from_disk,
    record_verify_outcome,
    record_verify_result,
    reset_verify_store,
    verify_finding,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path):
    reset_verify_store()
    hydrate_verify_from_disk(tmp_path)
    yield
    reset_verify_store()


def test_verify_skips_http_findings_field() -> None:
    assert _finding_looks_http({"http_exchange_ids": ["123"]}) is True
    assert _finding_looks_http({"http_request_id": "x"}) is True
    assert _finding_looks_http({"title": "logic"}) is False


@pytest.mark.asyncio
async def test_record_verify_outcome_requires_ownership() -> None:
    record_verify_result("f1", status="queued", agent_id="v1")
    coord = AgentCoordinator()
    await coord.register("h1", "Hunter", parent_id="root")
    await coord.register("v2", "Validator", parent_id="root")
    coord.metadata["h1"] = {"tier": "hunter"}
    coord.metadata["v2"] = {"tier": "validator"}

    async def _call(agent_id: str) -> dict[str, Any]:
        ctx = ToolContext(
            context={"coordinator": coord, "agent_id": agent_id},
            tool_name=record_verify_outcome.name,
            tool_call_id="c",
            tool_arguments="{}",
        )
        raw = await record_verify_outcome.on_invoke_tool(
            ctx,
            json.dumps(
                {"finding_id": "f1", "status": "upheld", "reason": "ok"}
            ),
        )
        return cast("dict[str, Any]", json.loads(raw))

    denied = await _call("h1")
    assert denied["success"] is False
    ok = await _call("v2")
    assert ok["success"] is True


@pytest.mark.asyncio
async def test_verify_spawn_failure_is_reported() -> None:
    state = MagicMock()
    state.get_existing_vulnerabilities.return_value = [
        {"id": "f9", "title": "logic bug", "endpoint": "/x"}
    ]
    spawner = AsyncMock(side_effect=RuntimeError("no slot"))
    ctx = ToolContext(
        context={"agent_id": "root", "spawn_child_agent": spawner},
        tool_name=verify_finding.name,
        tool_call_id="c",
        tool_arguments="{}",
    )
    with patch("Ben.report.get_global_report_state", return_value=state):
        raw = await verify_finding.on_invoke_tool(
            ctx,
            json.dumps(
                {
                    "finding_id": "f9",
                    "claim": "c",
                    "evidence": "e",
                    "falsifier": "f",
                }
            ),
        )
    result = json.loads(raw)
    assert result["success"] is False
    assert result["status"] == "not_spawned"


@pytest.mark.asyncio
async def test_verify_is_idempotent() -> None:
    state = MagicMock()
    state.get_existing_vulnerabilities.return_value = [
        {"id": "f1", "title": "t", "endpoint": "/x"}
    ]
    spawner = AsyncMock(return_value={"agent_id": "v1", "success": True})
    ctx = ToolContext(
        context={"agent_id": "root", "spawn_child_agent": spawner},
        tool_name=verify_finding.name,
        tool_call_id="c",
        tool_arguments="{}",
    )
    args = json.dumps(
        {
            "finding_id": "f1",
            "claim": "c",
            "evidence": "e",
            "falsifier": "f",
        }
    )
    with patch("Ben.report.get_global_report_state", return_value=state):
        first = json.loads(await verify_finding.on_invoke_tool(ctx, args))
        second = json.loads(await verify_finding.on_invoke_tool(ctx, args))
    assert first["success"] is True
    assert second.get("already") is True
    assert spawner.await_count == 1
    assert get_verify_result("f1")["status"] == "queued"
