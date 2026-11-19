"""BEN_COVERAGE_GATE strict vs warn."""

from __future__ import annotations

from typing import TYPE_CHECKING

from Ben.tools.coverage.tools import hydrate_coverage_from_disk, seed_pending_entries
from Ben.tools.finish.tool import _coverage_summary


if TYPE_CHECKING:
    from pathlib import Path


def test_warn_allows_pending(tmp_path: Path, monkeypatch) -> None:
    hydrate_coverage_from_disk(tmp_path)
    seed_pending_entries(
        [{"surface": "/a", "risk_area": "enum", "evidence": "inventory:t"}]
    )
    monkeypatch.setenv("BEN_COVERAGE_GATE", "warn")
    summary = _coverage_summary({})
    assert summary.get("success") is not False
    assert summary["pending_surfaces"]
    assert summary["coverage_gate"] == "warn"


def test_strict_blocks_pending(tmp_path: Path, monkeypatch) -> None:
    hydrate_coverage_from_disk(tmp_path)
    seed_pending_entries(
        [{"surface": "/a", "risk_area": "enum", "evidence": "inventory:t"}]
    )
    monkeypatch.setenv("BEN_COVERAGE_GATE", "strict")
    summary = _coverage_summary({})
    assert summary.get("success") is False
    assert summary["coverage_gate"] == "strict"
