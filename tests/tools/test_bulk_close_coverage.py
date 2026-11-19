"""受控 bulk_close_coverage：只关 pending→not_applicable，强制 evidence。"""

from __future__ import annotations

from pathlib import Path

from Ben.report.coverage import bulk_waivers_section, build_coverage_document
from Ben.tools.coverage.tools import (
    _bulk_close_impl,
    get_coverage_entries,
    hydrate_coverage_from_disk,
    seed_pending_entries,
)


def test_bulk_close_requires_evidence(tmp_path: Path) -> None:
    hydrate_coverage_from_disk(tmp_path)
    seed_pending_entries(
        [{"surface": "/static/a.css", "risk_area": "enum", "evidence": "inventory"}]
    )
    result = _bulk_close_impl(
        pattern=".css",
        outcome="not_applicable",
        evidence="too short",
        agent_id="root",
        agent_name="root",
    )
    assert result["success"] is False
    assert "evidence" in result["error"].lower()


def test_bulk_close_rejects_no_issue_found(tmp_path: Path) -> None:
    hydrate_coverage_from_disk(tmp_path)
    seed_pending_entries(
        [{"surface": "/x.css", "risk_area": "enum", "evidence": "inventory"}]
    )
    result = _bulk_close_impl(
        pattern=".css",
        outcome="no_issue_found",
        evidence="static assets outside injection scope for this pass",
        agent_id=None,
        agent_name=None,
    )
    assert result["success"] is False


def test_bulk_close_pending_and_report_waivers(tmp_path: Path) -> None:
    hydrate_coverage_from_disk(tmp_path)
    seed_pending_entries(
        [
            {"surface": "/a.css", "risk_area": "enum", "evidence": "inventory"},
            {"surface": "/b.css", "risk_area": "enum", "evidence": "inventory"},
            {"surface": "/admin", "risk_area": "auth", "evidence": "inventory"},
        ]
    )
    result = _bulk_close_impl(
        pattern=".css",
        outcome="not_applicable",
        evidence="static stylesheet assets; not in authorized attack surface",
        agent_id="root",
        agent_name="orchestrator",
    )
    assert result["success"] is True
    assert result["closed_count"] == 2
    entries = get_coverage_entries()
    css = [e for e in entries if str(e.get("surface", "")).endswith(".css")]
    assert all(e.get("outcome") == "not_applicable" for e in css)
    assert all(e.get("bulk_closed") is True for e in css)
    admin = next(e for e in entries if e.get("surface") == "/admin")
    assert admin.get("outcome") == "pending"

    waivers = bulk_waivers_section(entries)
    assert waivers["count"] == 2
    assert waivers["by_pattern"].get(".css") == 2

    doc = build_coverage_document(
        run_record={"run_id": "t", "status": "completed"},
        entries=entries,
        agent_graph={"statuses": {}, "names": {}, "metadata": {}},
        vulnerability_reports=[],
        exit_reason="finish_scan",
    )
    assert doc["summary"]["bulk_waivers"] == 2
    assert doc["bulk_waivers"]["count"] == 2
