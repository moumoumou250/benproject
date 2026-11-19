"""词表迁出：ALIASES / canonical / literal_lookup / audit。"""

from __future__ import annotations

from Ben.kb.vocab import ALIASES, audit, canonical, literal_lookup, normalize
from Ben.interface.cli import vuln as vuln_cli


def test_aliases_shared_with_vuln_cli():
    assert vuln_cli._ALIASES is ALIASES
    assert ALIASES["sqli"] == "sql-injection"
    assert ALIASES["lfi"] == "path-traversal-lfi-rfi"


def test_normalize_underscore_and_case():
    assert normalize("SQL_Injection") == "sql-injection"
    assert normalize(" path--traversal ") == "path-traversal"


def test_literal_lookup_rejects_stem_snake():
    """check 口径：sql_injection 字面不可达（叫法缺口）。"""
    assert literal_lookup("sql_injection") is None
    assert literal_lookup("sql-injection") == "sql-injection"
    assert literal_lookup("sqli") == "sql-injection"
    assert literal_lookup("IDOR") == "idor"


def test_canonical_resolves_stem_and_normalize():
    assert canonical("sql_injection") == "sql-injection"
    assert canonical("SQL-Injection") == "sql-injection"
    assert canonical("path_traversal_lfi_rfi") == "path-traversal-lfi-rfi"
    assert canonical("no-such-vuln-xyz") is None


def test_audit_has_no_orphans():
    issues = audit()
    orphans = [i for i in issues if i.kind == "orphan"]
    ambiguous = [i for i in issues if i.kind == "ambiguous"]
    assert orphans == [], orphans
    assert ambiguous == [], ambiguous
