"""编排埋点计数。"""

from __future__ import annotations

from pathlib import Path

import pytest

from Ben.core.orchestration_metrics import (
    bind_orchestration_metrics,
    coverage_duplicate_rate,
    record_coverage_duplicate,
    record_coverage_ok,
    record_root_tool_call,
    reset_orchestration_metrics,
    snapshot_orchestration_metrics,
)


def setup_function() -> None:
    reset_orchestration_metrics()


def teardown_function() -> None:
    reset_orchestration_metrics()


def test_metrics_snapshot_and_rate(tmp_path: Path) -> None:
    bind_orchestration_metrics(tmp_path)
    record_coverage_ok()
    record_coverage_ok()
    record_coverage_duplicate()
    record_root_tool_call("create_agent")
    record_root_tool_call("create_agent")
    record_root_tool_call("list_coverage")
    snap = snapshot_orchestration_metrics()
    assert snap["coverage_records_ok"] == 2
    assert snap["coverage_duplicate_rejects"] == 1
    assert snap["root_tool_calls"]["create_agent"] == 2
    assert coverage_duplicate_rate() == pytest.approx(1 / 3)
    assert (tmp_path / "orchestration_metrics.json").exists()
