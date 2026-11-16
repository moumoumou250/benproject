"""progress：内容签名、在飞豁免、N=0 关闭。"""

from __future__ import annotations

from Ben.core.agents import AgentCoordinator
from Ben.core.progress import (
    compute_world_signature,
    observe_progress,
    reset_progress_tracker,
    stall_streak,
)
from Ben.tools.coverage.tools import _coverage_lock, _coverage_storage


def _reset_coverage() -> None:
    with _coverage_lock:
        _coverage_storage.clear()


def setup_function() -> None:
    reset_progress_tracker()
    _reset_coverage()


def teardown_function() -> None:
    reset_progress_tracker()
    _reset_coverage()


def test_outcome_change_changes_signature() -> None:
    with _coverage_lock:
        _coverage_storage["e1"] = {
            "surface": "/a",
            "risk_area": "xss",
            "outcome": "needs_follow_up",
        }
    sig1 = compute_world_signature()
    with _coverage_lock:
        _coverage_storage["e1"]["outcome"] = "ruled_out"
    sig2 = compute_world_signature()
    assert sig1 != sig2


def test_active_children_defer_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_NO_PROGRESS_TURNS", "2")
    reset_progress_tracker()
    coord = AgentCoordinator()
    # sync register via statuses directly for unit test
    coord.statuses["root"] = "running"
    coord.parent_of["root"] = None
    coord.names["root"] = "Root"
    coord.statuses["c1"] = "running"
    coord.parent_of["c1"] = "root"
    coord.names["c1"] = "Child"

    first = observe_progress(coordinator=coord)
    second = observe_progress(coordinator=coord)
    assert first.get("deferred_active_children") or second.get("deferred_active_children")
    assert stall_streak() == 0
    assert second["should_wrap_up"] is False


def test_stall_wraps_after_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_NO_PROGRESS_TURNS", "2")
    reset_progress_tracker()
    coord = AgentCoordinator()
    coord.statuses["root"] = "running"
    coord.parent_of["root"] = None

    observe_progress(coordinator=coord)  # seed
    r2 = observe_progress(coordinator=coord)
    r3 = observe_progress(coordinator=coord)
    assert r2["stall_streak"] == 1
    assert r3["stall_streak"] == 2
    assert r3["should_wrap_up"] is True


def test_disabled_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_NO_PROGRESS_TURNS", "0")
    reset_progress_tracker()
    coord = AgentCoordinator()
    coord.statuses["root"] = "running"
    coord.parent_of["root"] = None
    for _ in range(5):
        obs = observe_progress(coordinator=coord)
        assert obs["should_wrap_up"] is False
        assert obs["enabled"] is False
