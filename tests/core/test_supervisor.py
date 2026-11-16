"""H1/H2/H3：supervisor 看护链路。"""

from __future__ import annotations

import asyncio

import pytest

from Ben.core.agents import AgentCoordinator
from Ben.core.supervisor import (
    DEFAULT_STALL_GRACE_S,
    _force_set_last_heartbeat_age_for_tests,
    bind_heartbeat,
    llm_in_flight,
    mark_llm_end,
    mark_llm_start,
    reset_supervisor_state,
    run_supervisor,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    reset_supervisor_state()
    bind_heartbeat(tmp_path)
    yield
    reset_supervisor_state()


@pytest.mark.asyncio
async def test_interactive_idle_does_not_stop() -> None:
    coord = AgentCoordinator()
    await coord.register("root", "Root", parent_id=None, task="t", skills=[])
    await coord.park_waiting("root", wait_kind="user")
    _force_set_last_heartbeat_age_for_tests(10_000.0)

    task = asyncio.create_task(
        run_supervisor(
            coordinator=coord,
            root_id="root",
            stall_timeout_s=1.0,
            poll_s=0.05,
            stall_grace_s=0.01,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coord.statuses["root"] == "waiting"


@pytest.mark.asyncio
async def test_supervisor_stops_hung_root() -> None:
    coord = AgentCoordinator()
    await coord.register("root", "Root", parent_id=None, task="t", skills=[])

    async def _hang() -> None:
        await asyncio.Event().wait()

    hang = asyncio.create_task(_hang())
    await coord.attach_runtime("root", task=hang)
    _force_set_last_heartbeat_age_for_tests(10_000.0)

    await run_supervisor(
        coordinator=coord,
        root_id="root",
        stall_timeout_s=1.0,
        poll_s=0.05,
        stall_grace_s=0.05,
    )
    assert hang.done()
    assert coord.statuses["root"] == "stopped"
    assert "heartbeat stalled" in str(coord.errors.get("root", ""))


def test_llm_inflight_survives_model_exception() -> None:
    mark_llm_start()
    assert llm_in_flight(stale_after_s=0) == 0
    mark_llm_start()
    mark_llm_start()
    assert llm_in_flight() == 2
    mark_llm_end()
    assert llm_in_flight() == 1


def test_default_grace_is_sixty() -> None:
    assert DEFAULT_STALL_GRACE_S == 60.0
