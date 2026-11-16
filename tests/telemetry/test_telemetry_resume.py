"""Regression tests for telemetry emitted by resumed runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agents.usage import Usage

from Ben.report.state import ReportState
from Ben.telemetry import posthog, scarf


def _usage(requests: int, input_tokens: int, output_tokens: int, total_tokens: int) -> Usage:
    return Usage(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


@pytest.mark.parametrize("telemetry", [posthog, scarf])
def test_resumed_run_reports_only_this_process_usage(
    telemetry: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续跑时只统计本进程的增量用量（遥测本身已是 no-op，但账仍要算对）。"""
    monkeypatch.chdir(tmp_path)
    initial = ReportState(run_name="resumed")
    initial.record_sdk_usage(
        agent_id="agent",
        usage=_usage(10, 1000, 200, 1200),
        model="unknown",
    )
    initial.record_observed_llm_cost(1.25)
    initial.end_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    initial.run_record["end_time"] = initial.end_time
    initial.save_run_data()

    resumed = ReportState(run_name="resumed")
    resumed.hydrate_from_run_dir()
    resumed.record_sdk_usage(
        agent_id="agent",
        usage=_usage(3, 300, 50, 350),
        model="unknown",
    )
    resumed.record_observed_llm_cost(0.75)

    assert telemetry.end(resumed) is None

    usage = resumed.get_process_llm_usage()
    assert usage["requests"] == 3
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 50
    assert usage["total_tokens"] == 350
    assert usage["cost"] == pytest.approx(0.75)
    assert 0 <= resumed.get_process_duration_seconds() <= 5


@pytest.mark.parametrize("telemetry", [posthog, scarf])
def test_fresh_run_reports_all_its_usage(telemetry: Any) -> None:
    state = ReportState()
    state.record_sdk_usage(
        agent_id="agent",
        usage=_usage(3, 300, 50, 350),
        model="unknown",
    )
    state.record_observed_llm_cost(0.75)

    assert telemetry.end(state) is None

    usage = state.get_process_llm_usage()
    assert usage["requests"] == 3
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 50
    assert usage["total_tokens"] == 350
    assert usage["cost"] == pytest.approx(0.75)
