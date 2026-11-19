"""BEN_* 开关：禁止「只写不读」的空 setdefault。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from Ben.kb.env import env_flag

_ROOT = Path(__file__).resolve().parents[2]
_BEN = _ROOT / "Ben"

# 尚未有 reader 的开关：产品代码禁止 setdefault。
_UNWIRED_FORBIDDEN = frozenset(
    {
        "BEN_AUDIT_REDACT",  # 脱敏仅导出侧；禁止写前涂
    }
)


def _product_py_files() -> list[Path]:
    return [p for p in _BEN.rglob("*.py") if p.is_file()]


def test_env_flag_spellings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BEN_KB_EVIDENCE", raising=False)
    assert env_flag("BEN_KB_EVIDENCE") is False
    assert env_flag("BEN_KB_EVIDENCE", default=True) is True
    for val in ("1", "true", "TRUE", "yes", "YES", "on"):
        monkeypatch.setenv("BEN_KB_EVIDENCE", val)
        assert env_flag("BEN_KB_EVIDENCE") is True
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("BEN_KB_EVIDENCE", val)
        assert env_flag("BEN_KB_EVIDENCE") is False


def test_ben_kb_evidence_has_reader():
    text = (_BEN / "core" / "runner.py").read_text(encoding="utf-8")
    assert "env_flag(\"BEN_KB_EVIDENCE\")" in text or "env_flag('BEN_KB_EVIDENCE')" in text


def test_ben_audit_has_reader():
    """G1 闭合：AuditWriter 路径必须读 BEN_AUDIT。"""
    text = (_BEN / "core" / "audit.py").read_text(encoding="utf-8")
    assert 'env_flag("BEN_AUDIT")' in text or "env_flag('BEN_AUDIT')" in text


def test_unwired_switches_not_set_in_product():
    for name in _UNWIRED_FORBIDDEN:
        set_pat = re.compile(rf"""setdefault\(\s*["']{name}["']""")
        hits = []
        for path in _product_py_files():
            if set_pat.search(path.read_text(encoding="utf-8")):
                hits.append(str(path.relative_to(_ROOT)))
        assert hits == [], f"{name} 仍被 setdefault：{hits}"


def test_removed_slop_keys_absent_from_kb():
    """验收：criteria_pass / technique_miss / write_exchange 不得再出现在 Ben/kb。"""
    banned = ("criteria_pass", "technique_miss", "def write_exchange")
    hits: list[str] = []
    for path in (_BEN / "kb").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            if token in text:
                hits.append(f"{path.relative_to(_ROOT)}:{token}")
    assert hits == []
