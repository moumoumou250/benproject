"""verify：假 runner 退出码矩阵 + oracle/profile 闸门。"""

from __future__ import annotations

from pathlib import Path

import pytest

from Ben.kb.contract import ExitCode, SubReason
from Ben.kb.criteria import Criterion
from Ben.kb.evidence import Evidence, EvidenceKind, surface_hit
from Ben.kb.verdict import build_verdicts, confirm, validate_verdict_payload
from Ben.kb.verify import ScanOutcome, verify_term


def test_confirm_empty_never_ok():
    r = confirm([], [], [], expects_vuln=True, surface_hit=True, blocked=False)
    assert r.code == ExitCode.INCONCLUSIVE
    assert r.subreason == SubReason.NO_VALIDATION


def test_confirm_blocked_not_downgraded():
    criteria = [Criterion(1, "Demonstrate code execution via oracle", "numbered", True)]
    verdicts = build_verdicts(criteria, expected_indices=[1], mechanical={1: ("FAIL", [])})
    r = confirm(
        criteria,
        verdicts,
        [],
        expects_vuln=True,
        surface_hit=True,
        blocked=True,
    )
    assert r.code == ExitCode.NEGATIVE
    assert r.subreason == SubReason.BLOCKED


def test_confirm_miss_surface_before_false_negative():
    criteria = [Criterion(1, "Demonstrate code execution via oracle", "numbered", True)]
    verdicts = build_verdicts(criteria, expected_indices=[1], mechanical={1: ("FAIL", ["http/x.req"])})
    r = confirm(
        criteria,
        verdicts,
        [Evidence("http/x.req", EvidenceKind.E2, url_path="/other")],
        expects_vuln=True,
        surface_hit=False,
        blocked=False,
    )
    assert r.subreason == SubReason.MISS_SURFACE


def test_confirm_ok_requires_e1e2_refs():
    criteria = [Criterion(1, "Demonstrate code execution via oracle proof", "numbered", True)]
    verdicts = build_verdicts(
        criteria,
        expected_indices=[1],
        mechanical={1: ("PASS", ["http/001.req"])},
    )
    r = confirm(
        criteria,
        verdicts,
        [Evidence("http/001.req", EvidenceKind.E2, method="GET", url_path="/actuator/gateway/routes/x")],
        expects_vuln=True,
        surface_hit=True,
        blocked=False,
    )
    assert r.code == ExitCode.OK


def test_confirm_rejects_e3_kind_even_if_http_path():
    criteria = [Criterion(1, "Demonstrate code execution via oracle proof", "numbered", True)]
    verdicts = build_verdicts(
        criteria,
        expected_indices=[1],
        mechanical={1: ("PASS", ["http/evil.req"])},
    )
    r = confirm(
        criteria,
        verdicts,
        [Evidence("http/evil.req", EvidenceKind.E3, url_path="/actuator/gateway/routes")],
        expects_vuln=True,
        surface_hit=True,
        blocked=False,
    )
    assert r.code == ExitCode.INCONCLUSIVE
    assert r.subreason == SubReason.UNREADABLE


def test_surface_hit_ignores_oracle_dir():
    ev = [
        Evidence("oracle/precheck.req", EvidenceKind.E2, url_path="/actuator/gateway/routes/ben-kb-oracle"),
        Evidence("http/001.req", EvidenceKind.E2, url_path="/actuator/health"),
    ]
    assert surface_hit(ev, "/actuator/gateway/routes/ben-kb-oracle") is False
    ev.append(
        Evidence("http/002.req", EvidenceKind.E2, url_path="/actuator/gateway/routes/ben-kb-oracle")
    )
    assert surface_hit(ev, "/actuator/gateway/routes/ben-kb-oracle") is True


def test_verify_usage_without_profile():
    r = verify_term("rce", profile_name="")
    assert r.code == ExitCode.USAGE


def _enable_scg_whitelist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "ben.env"
    env.write_text(
        "ALLOWED_TARGETS=http://127.0.0.1:8083/,http://172.17.0.1:8083/\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEN_ENV_FILE", str(env))


def _scg_up() -> bool:
    try:
        import httpx

        return httpx.get("http://127.0.0.1:8083/actuator/health", timeout=2).status_code == 200
    except Exception:
        return False


def test_verify_fake_runner_miss_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not _scg_up():
        pytest.skip("SCG 靶场未就绪")
    _enable_scg_whitelist(tmp_path, monkeypatch)

    def fake_scan(**_kwargs):
        return ScanOutcome(
            stopped_code=__import__("Ben.core.stopped", fromlist=["StoppedCode"]).StoppedCode.FINISHED,
            stopped_reason="finish_scan",
            run_name="fake",
            evidence=(
                Evidence(
                    "http/001.req",
                    EvidenceKind.E2,
                    method="GET",
                    url_path="/actuator/health",
                    status=200,
                ),
            ),
            mechanical={},
        )

    r = verify_term(
        "rce",
        profile_name="local-scg-rce-positive",
        out_dir=tmp_path / "ev",
        run_scan=fake_scan,
    )
    assert r.code == ExitCode.NEGATIVE
    assert r.subreason == SubReason.MISS_SURFACE


def test_verify_no_auto_mechanical_without_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """无显式 mechanical 时，仅有 e2+hit 不得自动 CONFIRMED。"""
    if not _scg_up():
        pytest.skip("SCG 靶场未就绪")
    _enable_scg_whitelist(tmp_path, monkeypatch)
    from Ben.core.stopped import StoppedCode

    def fake_scan(**_kwargs):
        return ScanOutcome(
            stopped_code=StoppedCode.FINISHED,
            run_name="fake",
            evidence=(
                Evidence(
                    "http/002.req",
                    EvidenceKind.E2,
                    method="GET",
                    url_path="/actuator/gateway/routes/ben-kb-oracle",
                    status=200,
                ),
            ),
            mechanical=None,
        )

    r = verify_term(
        "rce",
        profile_name="local-scg-rce-positive",
        out_dir=tmp_path / "ev_no_mech",
        run_scan=fake_scan,
    )
    assert r.code != ExitCode.OK
    assert r.subreason == SubReason.UNREADABLE


def test_verify_fake_runner_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not _scg_up():
        pytest.skip("SCG 靶场未就绪")
    _enable_scg_whitelist(tmp_path, monkeypatch)
    from Ben.core.stopped import StoppedCode

    def fake_scan(**_kwargs):
        return ScanOutcome(
            stopped_code=StoppedCode.FINISHED,
            run_name="fake",
            evidence=(
                Evidence(
                    "http/002.req",
                    EvidenceKind.E2,
                    method="GET",
                    url_path="/actuator/gateway/routes/ben-kb-oracle",
                    status=200,
                ),
            ),
            mechanical={1: ("PASS", ["http/002.req"])},
        )

    r = verify_term(
        "rce",
        profile_name="local-scg-rce-positive",
        out_dir=tmp_path / "ev2",
        run_scan=fake_scan,
    )
    assert r.code == ExitCode.OK, (r.message, r.subreason, r.extra)
    assert validate_verdict_payload(r.extra or {}) == []


def test_blocked_rglob_nested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not _scg_up():
        pytest.skip("SCG 靶场未就绪")
    _enable_scg_whitelist(tmp_path, monkeypatch)
    from Ben.core.stopped import StoppedCode

    ev_dir = tmp_path / "ev_block"
    nested = ev_dir / "http" / "nested"
    nested.mkdir(parents=True)
    (nested / "x.req").write_text("GET /actuator/health HTTP/1.1\n", encoding="utf-8")
    (nested / "x.resp").write_text(
        "HTTP/1.1 403\n\nAttention Required! Cloudflare\n", encoding="utf-8"
    )

    def fake_scan(**_kwargs):
        return ScanOutcome(stopped_code=StoppedCode.FINISHED, run_name=None, mechanical={})

    # 把预置 http 放进 out_dir；verify 会 collect
    r = verify_term(
        "rce",
        profile_name="local-scg-rce-positive",
        out_dir=ev_dir,
        run_scan=fake_scan,
    )
    assert r.subreason == SubReason.BLOCKED
