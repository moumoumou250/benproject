"""pending outcome + inventory seeding."""

from __future__ import annotations

from typing import TYPE_CHECKING

from Ben.tools.coverage.tools import (
    _list_impl,
    _record_impl,
    _update_impl,
    get_coverage_entries,
    hydrate_coverage_from_disk,
    outcome_counts,
    seed_pending_entries,
)


if TYPE_CHECKING:
    from pathlib import Path


def test_seed_pending_idempotent(tmp_path: Path) -> None:
    hydrate_coverage_from_disk(tmp_path)
    items = [
        {
            "surface": "http://t/admin",
            "risk_area": "auth",
            "evidence": "inventory:paths status=401",
        },
        {
            "surface": "http://t/admin",
            "risk_area": "auth",
            "evidence": "duplicate",
        },
    ]
    assert seed_pending_entries(items) == 1
    assert seed_pending_entries(items) == 0
    assert outcome_counts() == {"pending": 1}
    listed = _list_impl(outcome="pending", surface=None, caller_agent_id=None)
    assert listed["filtered_count"] == 1


def test_agent_cannot_record_pending(tmp_path: Path) -> None:
    hydrate_coverage_from_disk(tmp_path)
    result = _record_impl(
        surface="/x",
        risk_area="enum",
        outcome="pending",
        evidence="nope",
        agent_id="a1",
        agent_name="t",
    )
    assert result["success"] is False
    assert not get_coverage_entries()


def test_update_closes_pending(tmp_path: Path) -> None:
    hydrate_coverage_from_disk(tmp_path)
    seed_pending_entries(
        [{"surface": "/api", "risk_area": "authz", "evidence": "inventory:x"}]
    )
    eid = get_coverage_entries()[0]["entry_id"]
    result = _update_impl(
        entry_id=eid,
        outcome="no_issue_found",
        evidence="probed",
        agent_id="a1",
        agent_name="hunter",
    )
    assert result["success"] is True
    assert get_coverage_entries()[0]["outcome"] == "no_issue_found"
