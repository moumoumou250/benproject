"""general 并发帽与 tier 归一。"""

from __future__ import annotations

import pytest

from Ben.core.agents import AgentCoordinator
from Ben.core.execution import _active_tier_count, _normalize_tier


def test_normalize_tier() -> None:
    assert _normalize_tier(None) == "hunter"
    assert _normalize_tier("GENERAL") == "general"
    with pytest.raises(RuntimeError):
        _normalize_tier("elite")


def test_active_tier_count() -> None:
    coord = AgentCoordinator()
    coord.statuses["root"] = "running"
    coord.parent_of["root"] = None
    coord.metadata["root"] = {}
    coord.statuses["g1"] = "running"
    coord.parent_of["g1"] = "root"
    coord.metadata["g1"] = {"tier": "general"}
    coord.statuses["h1"] = "running"
    coord.parent_of["h1"] = "root"
    coord.metadata["h1"] = {"tier": "hunter"}
    coord.statuses["g2"] = "completed"
    coord.parent_of["g2"] = "root"
    coord.metadata["g2"] = {"tier": "general"}
    assert _active_tier_count(coord, "general") == 1
    assert _active_tier_count(coord, "hunter") == 1
