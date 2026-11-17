"""Intent reaper：终态 reopen + resume 清扫。"""

from __future__ import annotations

from pathlib import Path

import pytest

from Ben.core.agents import AgentCoordinator
from Ben.core.task_graph import (
    add_fact,
    claim_intent,
    get_intent,
    hydrate_task_graph_from_disk,
    peek_ready,
    release_intents_for_agent,
    reset_task_graph,
    sweep_stale_claimed_intents,
)


def setup_function() -> None:
    reset_task_graph()


@pytest.mark.asyncio
async def test_set_status_stop_reopens_claimed_intent(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    fact = add_fact(kind="host", title="h", data={"host": "h"})
    intent_id = fact["intent_id"]
    assert intent_id
    claim_intent(intent_id, worker="child1")
    assert get_intent(intent_id)["status"] == "claimed"

    coordinator = AgentCoordinator()
    await coordinator.register("root", "Root", parent_id=None)
    await coordinator.register("child1", "Child", parent_id="root")
    coordinator.metadata["child1"]["intent_id"] = intent_id

    await coordinator.set_status("child1", "stopped", error="timebox")
    assert get_intent(intent_id)["status"] == "open"
    assert get_intent(intent_id)["worker"] is None
    ready_ids = {r["id"] for r in peek_ready()}
    assert intent_id in ready_ids


@pytest.mark.asyncio
async def test_completed_after_conclude_does_not_reopen(tmp_path: Path) -> None:
    from Ben.core.task_graph import conclude_intent

    hydrate_task_graph_from_disk(tmp_path)
    fact = add_fact(kind="host", title="h2", data={"host": "h2"})
    intent_id = fact["intent_id"]
    claim_intent(intent_id, worker="child2")
    conclude_intent(intent_id, status="done")

    coordinator = AgentCoordinator()
    await coordinator.register("root", "Root", parent_id=None)
    await coordinator.register("child2", "Child", parent_id="root")
    coordinator.metadata["child2"]["intent_id"] = intent_id

    await coordinator.set_status("child2", "completed")
    assert get_intent(intent_id)["status"] == "done"


def test_sweep_stale_claimed_on_terminal_worker(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    fact = add_fact(kind="host", title="h3", data={"host": "h3"})
    intent_id = fact["intent_id"]
    claim_intent(intent_id, worker="dead")
    swept = sweep_stale_claimed_intents(
        live_agent_ids={"root"},
        terminal_agent_ids={"dead"},
    )
    assert intent_id in swept
    assert get_intent(intent_id)["status"] == "open"


def test_release_intents_for_agent_batch(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    a = add_fact(kind="host", title="ha", data={"host": "ha"})
    b = add_fact(kind="service", title="80", data={"host": "ha", "port": "80", "proto": "tcp"})
    ids = [a["intent_id"], b["intent_id"]]
    for iid in ids:
        claim_intent(iid, worker="w")
    reopened = release_intents_for_agent("w", intent_ids=ids)
    assert set(reopened) == set(ids)
