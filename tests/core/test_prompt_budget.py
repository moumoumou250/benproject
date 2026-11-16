"""事实索引与截断口径。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from Ben.core.prompt_budget import (
    INDEX_LINE_MAX,
    LIST_PREVIEW_CHARS,
    build_shared_findings_index,
    clip_line,
)
from Ben.tools.notes import tools as notes_mod


def test_clip_line_flattens_and_truncates() -> None:
    assert clip_line("a\nb\tc") == "a b c"
    long = "word " * 40
    out = clip_line(long, 40)
    assert len(out) <= 40
    assert out.endswith("...")


def test_build_shared_findings_index_from_reports_and_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_state = MagicMock()
    report_state.get_existing_vulnerabilities.return_value = [
        {
            "id": "vuln-0001",
            "severity": "high",
            "title": "IDOR on /api/users",
            "endpoint": "/api/users",
        }
    ]
    monkeypatch.setattr(
        "Ben.report.state.get_global_report_state",
        lambda: report_state,
    )

    notes_mod._notes_storage.clear()
    notes_mod._notes_storage["abc"] = {
        "note_id": "abc",
        "title": "Login rate limit absent",
        "category": "findings",
        "content": "Tried 100 req/s, no 429",
        "updated_at": "2026-01-01",
    }
    try:
        text = build_shared_findings_index()
    finally:
        notes_mod._notes_storage.clear()

    assert "vuln-0001" in text
    assert "IDOR" in text
    assert "note:findings #abc" in text
    for line in text.splitlines():
        assert len(line) <= INDEX_LINE_MAX + 20  # 前缀略超单字段上限可接受


def test_notes_preview_uses_budget_constant() -> None:
    assert notes_mod._DEFAULT_CONTENT_PREVIEW_CHARS == LIST_PREVIEW_CHARS
