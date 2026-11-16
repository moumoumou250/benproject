"""H4：finish_scan 消费 verify 队列。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from Ben.tools.finish.tool import _do_finish
from Ben.tools.verify.tool import (
    hydrate_verify_from_disk,
    record_verify_result,
    reset_verify_store,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path):
    reset_verify_store()
    hydrate_verify_from_disk(tmp_path)
    yield
    reset_verify_store()


def test_verify_refuted_is_excluded_from_report() -> None:
    report_state = MagicMock()
    report_state.vulnerability_reports = [
        {"id": "f1", "title": "Bug A"},
        {"id": "f2", "title": "Bug B"},
    ]
    record_verify_result("f1", status="refuted", reason="false positive")
    record_verify_result("f2", status="upheld", reason="ok")

    with patch(
        "Ben.report.state.get_global_report_state", return_value=report_state
    ), patch(
        "Ben.tools.finish.tool._coverage_summary",
        return_value={"coverage_recorded": 0},
    ):
        result = _do_finish(
            parent_id=None,
            executive_summary="exec",
            methodology="meth",
            technical_analysis="tech",
            recommendations="rec",
            agent_graph={},
        )

    assert result["success"] is True
    assert result["refuted_ids"] == ["f1"]
    assert result["vulnerabilities_found"] == 2
    assert result["vulnerabilities_effective"] == 1
    assert report_state.vulnerability_reports[0]["verification_status"] == "refuted"
    assert "[已证伪]" in report_state.vulnerability_reports[0]["title"]
    tech_arg = report_state.update_scan_final_fields.call_args.kwargs[
        "technical_analysis"
    ]
    assert "Verification appendix" in tech_arg
    assert "f1" in tech_arg
