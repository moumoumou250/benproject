"""ben skill check / diagnose 退出码与自洽性。"""

from __future__ import annotations

from Ben.kb.attribution import diagnose_term
from Ben.kb.contract import Attribution, ExitCode
from Ben.kb.criteria import extract, is_checkable_text
from Ben.kb.inventory import check_term
from Ben.interface.cli.skill import run_skill


def test_check_all_strict_green():
    result = check_term(None, strict=True, all_terms=True)
    assert result.code == ExitCode.OK
    assert len(result.rows) == 29
    assert all(r["reachable"] for r in result.rows)


def test_check_sql_injection_stem_unreachable():
    result = check_term("sql_injection")
    assert result.code == ExitCode.NEGATIVE
    assert result.rows[0]["reachable"] is False
    assert "叫法缺口" in str(result.rows[0]["detail"])


def test_check_idor_reachable():
    result = check_term("idor")
    assert result.code == ExitCode.OK
    assert "Validation" in str(result.rows[0]["detail"])


def test_diagnose_sql_injection_naming_gap():
    result = diagnose_term("sql_injection")
    assert result.code == ExitCode.OK
    assert result.attribution == Attribution.NAMING_GAP
    assert result.extra.get("file")
    assert result.action and result.action["class"] in {"C", "D"}


def test_diagnose_sql_hyphen_reachable():
    result = diagnose_term("sql-injection")
    assert result.code == ExitCode.OK
    assert result.attribution is None
    assert result.rows[0]["checkable"] >= 1


def test_diagnose_csrf_variants_weak_but_ok():
    """D3：csrf 无别名且 stem==name，variants 可能为 1，仍可达。"""
    result = diagnose_term("csrf")
    assert result.code == ExitCode.OK
    assert result.attribution is None


def test_check_diagnose_consistent_on_stem():
    """§6.3 自洽：check 1 + diagnose 0（叫法缺口）。"""
    c = check_term("sql_injection")
    d = diagnose_term("sql_injection")
    assert c.code == ExitCode.NEGATIVE
    assert d.code == ExitCode.OK
    assert d.attribution == Attribution.NAMING_GAP


def test_term_and_all_conflict_via_cli():
    assert run_skill(["check", "idor", "--all"]) == int(ExitCode.USAGE)


def test_is_checkable_skips_process_sentences():
    assert is_checkable_text("Provide reproducible steps and evidence") is False
    assert (
        is_checkable_text(
            "Demonstrate access to an object not owned by the current user"
        )
        is True
    )


def test_extract_validation_from_idor():
    from Ben.skills import get_skill

    body = get_skill("vulnerabilities/idor")
    criteria = extract(body, "Validation")
    assert len(criteria) >= 3
    assert any(c.checkable for c in criteria)
