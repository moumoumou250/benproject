"""finding→e3、agents.db paths_seen、stopped_code、surface_hit 仅 e1/e2。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from Ben.core.stopped import StoppedCode, coerce_stopped_code, stopped_code_from_reason
from Ben.kb.contract import ExitCode, SubReason
from Ben.kb.criteria import Criterion
from Ben.kb.evidence import (
    Evidence,
    EvidenceKind,
    collect_from_http_dir,
    materialize_finding_exchanges,
    parse_req_first_line,
    paths_seen,
    paths_seen_from_agents_db,
    surface_hit,
)
from Ben.kb.evidence_ingest import merge_workspace_evidence
from Ben.kb.profile import load_profile
from Ben.kb.verdict import build_verdicts, confirm


def test_stopped_code_enum_roundtrip():
    assert coerce_stopped_code("budget") is StoppedCode.BUDGET
    assert coerce_stopped_code("nope") is StoppedCode.UNKNOWN
    assert stopped_code_from_reason("预算耗尽") is StoppedCode.BUDGET
    assert stopped_code_from_reason("完成扫描（finish_scan）") is StoppedCode.FINISHED
    # 不再把任意非空抬成 finished
    assert stopped_code_from_reason("随便一句话") is StoppedCode.UNKNOWN


def test_finding_http_fence_is_e3_not_surface(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "vulnerabilities.json").write_text(
        json.dumps(
            [
                {
                    "id": "v1",
                    "method": "POST",
                    "endpoint": "http://172.17.0.1:8083/actuator/gateway/routes",
                    "evidence": (
                        "```http\n"
                        "POST /actuator/gateway/routes HTTP/1.1\n"
                        "Host: 172.17.0.1:8083\n"
                        "\n"
                        "{\"id\":\"x\"}\n"
                        "HTTP/1.1 201 Created\n"
                        "\n"
                        "ok\n"
                        "```"
                    ),
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "findings_http"
    ev = materialize_finding_exchanges(run_dir, dest)
    assert ev
    assert all(e.kind == EvidenceKind.E3 for e in ev)
    assert all(e.path.startswith("findings_http/") for e in ev)
    assert (dest / Path(ev[0].path).name).is_file()
    # e3 不得算 surface_hit
    assert surface_hit(ev, "/actuator/gateway/routes") is False
    assert paths_seen(ev) == []


def test_surface_hit_e2_positive():
    ev = [
        Evidence(
            "http/001.req",
            EvidenceKind.E2,
            method="GET",
            url_path="/actuator/gateway/routes",
        )
    ]
    assert surface_hit(ev, "/actuator/gateway/routes") is True


def test_confirm_rejects_findings_http_refs():
    criteria = [Criterion(1, "Demonstrate code execution via oracle proof", "numbered", True)]
    verdicts = build_verdicts(
        criteria,
        expected_indices=[1],
        mechanical={1: ("PASS", ["findings_http/001.req"])},
    )
    r = confirm(
        criteria,
        verdicts,
        [Evidence("findings_http/001.req", EvidenceKind.E3, url_path="/actuator/gateway/routes")],
        expects_vuln=True,
        surface_hit=True,
        blocked=False,
    )
    assert r.code == ExitCode.INCONCLUSIVE
    assert r.subreason == SubReason.UNREADABLE


def test_paths_seen_from_agents_db(tmp_path: Path, monkeypatch):
    from Ben.core import paths as paths_mod

    run_dir = tmp_path / "run"
    state = run_dir / ".state"
    state.mkdir(parents=True)
    db = state / "agents.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table agent_messages (id integer primary key, session_id text, message_data text)"
    )
    rows = [
        (
            "root",
            json.dumps(
                {
                    "type": "function_call",
                    "arguments": '{"command":"curl http://172.17.0.1:8083/actuator/gateway/routes"}',
                }
            ),
        ),
        (
            "root",
            json.dumps(
                {
                    "type": "function_call_output",
                    "output": "See https://cdn.example/docs/api/users/102 for docs",
                }
            ),
        ),
        (
            "root",
            json.dumps(
                {
                    "type": "reasoning",
                    "content": "maybe try http://172.17.0.1:8083/secret/admin later",
                }
            ),
        ),
    ]
    conn.executemany(
        "insert into agent_messages(session_id, message_data) values (?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(paths_mod, "runtime_state_dir", lambda _d: state)
    seen = paths_seen_from_agents_db(run_dir)
    assert "/actuator/gateway/routes" in seen
    assert "/docs/api/users/102" not in seen
    assert "/secret/admin" not in seen


def test_collect_from_http_dir_recursive(tmp_path: Path):
    http = tmp_path / "http"
    nested = http / "nested"
    nested.mkdir(parents=True)
    (http / "001_GET_root.req").write_text("GET /actuator/health HTTP/1.1\n", encoding="utf-8")
    (http / "001_GET_root.resp").write_text("HTTP/1.1 200 OK\n\n", encoding="utf-8")
    (nested / "002_POST_routes.req").write_text(
        "POST /actuator/gateway/routes HTTP/1.1\n", encoding="utf-8"
    )
    (nested / "002_POST_routes.resp").write_text("HTTP/1.1 201 Created\n\n", encoding="utf-8")
    ev = collect_from_http_dir(http, kind=EvidenceKind.E2)
    paths = {e.url_path for e in ev}
    assert "/actuator/health" in paths
    assert "/actuator/gateway/routes" in paths
    assert any(e.path.endswith("nested/002_POST_routes.req") for e in ev)


def test_parse_req_first_line():
    assert parse_req_first_line("GET /api/users HTTP/1.1\n") == ("GET", "/api/users")
    assert parse_req_first_line("POST https://h/x\n") == ("POST", "https://h/x")
    assert parse_req_first_line("not a request") is None


def test_merge_workspace_evidence_nested(tmp_path: Path):
    run_dir = tmp_path / "run"
    nested = run_dir / ".evidence" / "http"
    nested.mkdir(parents=True)
    (nested / "001.req").write_text("GET /actuator/health HTTP/1.1\n", encoding="utf-8")
    (nested / "001.resp").write_text("HTTP/1.1 200\n\n", encoding="utf-8")
    http_dir = tmp_path / "http"
    ev = merge_workspace_evidence(run_dir, http_dir)
    assert (http_dir / "http" / "001.req").is_file()
    assert any(e.url_path == "/actuator/health" for e in ev)
    assert all(e.kind == EvidenceKind.E2 for e in ev)


def test_profile_verify_hint_loaded():
    p = load_profile("local-scg-rce-positive")
    assert "CVE-2022-22947" in p.verify_hint
    import Ben.kb.verify as verify_mod

    src = Path(verify_mod.__file__).read_text(encoding="utf-8")
    assert "CVE-2022-22947 / gateway actuator SpEL" not in src
    assert "verify_hint" in src
    assert "setdefault(\"BEN_AUDIT\"" not in src
    assert 'setdefault("BEN_AUDIT"' not in src
    assert "os.environ.setdefault(\"BEN_AUDIT\"" not in src


def test_kb_no_bare_finished_string():
    """Ben/kb 禁止裸 \"finished\" 字面量（应用 StoppedCode）。"""
    root = Path(__file__).resolve().parents[2] / "Ben" / "kb"
    bad: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if '"finished"' in text or "'finished'" in text:
            bad.append(str(path.relative_to(root.parent.parent)))
    assert bad == []
