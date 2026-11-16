"""task_claims：认领闸门、TTL、terminal 释放、BEN_CLAIM 旁路。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from Ben.core.agents import AgentCoordinator
from Ben.core.task_claims import (
    claims_enabled,
    hydrate_task_claims_from_disk,
    list_claims,
    owner_of,
    release_for_agent,
    reset_task_claims,
    try_claim,
)


@pytest.fixture(autouse=True)
def _clean_claims(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reset_task_claims()
    hydrate_task_claims_from_disk(tmp_path)
    yield
    reset_task_claims()


def test_claims_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEN_CLAIM", raising=False)
    assert claims_enabled() is False
    result = try_claim(" /api ", "xss", agent_id="a1", ttl_minutes=1)
    assert result["success"] is True
    assert result.get("bypassed") is True
    assert owner_of("/api", "xss") is None


def test_claim_and_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_CLAIM", "1")
    ok = try_claim("/api/login", "xss", agent_id="a1", agent_name="Hunter")
    assert ok["success"] is True
    assert owner_of("/api/login", "XSS") == "a1"
    conflict = try_claim("/api/login", "xss", agent_id="a2")
    assert conflict["success"] is False
    assert conflict["holder_agent_id"] == "a1"


def test_same_owner_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_CLAIM", "1")
    assert try_claim("s", "r", agent_id="a1")["success"]
    assert try_claim("s", "r", agent_id="a1")["success"]


def test_release_for_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_CLAIM", "1")
    try_claim("a", "b", agent_id="x")
    try_claim("c", "d", agent_id="x")
    released = release_for_agent("x")
    assert len(released) == 2
    assert list_claims() == []


@pytest.mark.asyncio
async def test_terminal_status_releases_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_CLAIM", "1")
    coord = AgentCoordinator()
    await coord.register("child1", "C", "root", task="t")
    try_claim("surf", "risk", agent_id="child1")
    assert owner_of("surf", "risk") == "child1"
    await coord.set_status("child1", "completed")
    assert owner_of("surf", "risk") is None


def test_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_CLAIM", "1")
    try_claim("s", "r", agent_id="a1", ttl_minutes=0.00001)
    time.sleep(0.05)
    # purge on next try_claim / owner_of
    assert owner_of("s", "r") is None
    assert try_claim("s", "r", agent_id="a2")["success"]
