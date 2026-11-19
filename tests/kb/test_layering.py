"""三层不越界：check / diagnose 零网络、零写盘，且不导入 kb.oracle。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from Ben.kb.attribution import diagnose_term
from Ben.kb.inventory import check_term

_KB = Path(__file__).resolve().parents[2] / "Ben" / "kb"
_FORBIDDEN_IMPORTS = {
    "requests",
    "httpx",
    "aiohttp",
    "Ben.tools.proxy",
    "Ben.kb.oracle",
    "Ben.kb.verify",
}


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
                found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize(
    "module",
    ["inventory.py", "attribution.py", "criteria.py", "schema.py", "vocab.py", "actions.py"],
)
def test_check_diagnose_modules_forbid_network_imports(module: str):
    path = _KB / module
    assert path.is_file()
    imports = _imports_in(path)
    bad = imports & _FORBIDDEN_IMPORTS
    assert not bad, f"{module} 禁止导入 {bad}"


def test_lower_layers_do_not_import_verify():
    for module in ("inventory.py", "attribution.py", "criteria.py", "vocab.py"):
        imports = _imports_in(_KB / module)
        assert "Ben.kb.verify" not in imports
        assert "verify" not in imports


def test_audit_module_layering():
    """audit 不得 import tools/report/interface。"""
    audit = Path(__file__).resolve().parents[2] / "Ben" / "core" / "audit.py"
    imports = _imports_in(audit)
    for banned in ("Ben.tools", "Ben.report", "Ben.interface"):
        assert banned not in imports
        assert not any(i.startswith(banned + ".") for i in imports)


def test_check_diagnose_zero_writes(monkeypatch):
    writes: list[str] = []

    def boom_write(*_a, **_k):
        writes.append("write_text")
        raise AssertionError("check/diagnose must not write")

    def boom_mkdir(*_a, **_k):
        writes.append("mkdir")
        raise AssertionError("check/diagnose must not mkdir")

    monkeypatch.setattr(Path, "write_text", boom_write)
    monkeypatch.setattr(Path, "mkdir", boom_mkdir)

    check_term("idor")
    check_term(None, all_terms=True, strict=True)
    diagnose_term("sql_injection")
    diagnose_term("idor")
    assert writes == []
