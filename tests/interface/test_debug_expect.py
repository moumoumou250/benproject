"""``--expect`` 匹配与调试摘要。"""

from __future__ import annotations

from types import SimpleNamespace

from Ben.interface.cli.debug_expect import (
    EXIT_EXPECT_MISS,
    count_gate_denials,
    finding_matches_expect,
    format_debug_summary,
    match_expect,
    normalize_expect_patterns,
)


def test_normalize_expect_splits_and_dedupes() -> None:
    assert normalize_expect_patterns(["IDOR, /api/x", "idor", " XSS "]) == [
        "IDOR",
        "/api/x",
        "XSS",
    ]


def test_finding_matches_endpoint_and_cwe() -> None:
    report = {
        "id": "vuln-0001",
        "title": "Broken access control",
        "endpoint": "/api/users/2",
        "cwe": "CWE-639",
        "severity": "high",
    }
    assert finding_matches_expect(report, "users/2")
    assert finding_matches_expect(report, "cwe-639")
    assert not finding_matches_expect(report, "sqli")


def test_match_expect_requires_all_patterns() -> None:
    reports = [
        {"id": "vuln-0001", "title": "IDOR on /api/users", "endpoint": "/api/users"},
        {"id": "vuln-0002", "title": "Reflected XSS", "endpoint": "/search"},
    ]
    ok, missing, hits = match_expect(reports, ["IDOR", "XSS"])
    assert ok and not missing
    assert hits["IDOR"] == ["vuln-0001"]
    assert hits["XSS"] == ["vuln-0002"]

    ok2, missing2, _ = match_expect(reports, ["IDOR", "SSRF"])
    assert not ok2
    assert missing2 == ["SSRF"]


def test_format_debug_summary_shows_miss() -> None:
    text = format_debug_summary(
        run_name="demo",
        findings=[{"id": "vuln-0001", "severity": "high", "title": "IDOR"}],
        expect_patterns=["IDOR", "SSRF"],
        expect_ok=False,
        expect_missing=["SSRF"],
        expect_hits={"IDOR": ["vuln-0001"], "SSRF": []},
        invocations=[
            SimpleNamespace(name="create_agent", output="ok"),
            SimpleNamespace(name="exec_command", output="不在授权白名单内"),
        ],
    )
    assert "vuln-0001" in text
    assert "MISS" in text
    assert f"exit {EXIT_EXPECT_MISS}" in text
    assert "gate-like refusals" in text
    assert count_gate_denials(
        [SimpleNamespace(output="BEN_MAX_SUBAGENTS=1 reached")]
    ) == 1
