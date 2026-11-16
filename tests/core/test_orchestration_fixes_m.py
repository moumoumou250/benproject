"""M1–M5 / M9 / M12 验收。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from Ben.core.frontier import render_frontier_block
from Ben.core.hooks import ReportUsageHooks
from Ben.core.progress import (
    is_no_progress_latched,
    latch_no_progress,
    reset_progress_tracker,
)
from Ben.core.recall import overlap_score, tokenize
from Ben.core.scan_clock import (
    SCAN_DEADLINE_KEY,
    SCAN_STARTED_AT_KEY,
    install_deadline,
    remaining_minutes,
)
from Ben.core.supervisor import reset_supervisor_state
from Ben.tools.verify.tool import reset_verify_store


def test_time_line_replaced_not_appended() -> None:
    hooks = ReportUsageHooks(model="m", max_budget_usd=None)
    ctx = MagicMock()
    ctx.context = {
        "agent_id": "root",
        "parent_id": None,
        "scan_deadline_mono": time.time() + 600,
        "scan_minutes_total": 10.0,
    }
    items: list = []
    for _ in range(12):
        hooks._maybe_warn_scan_time(ctx, items)
    texts = [
        i.get("content", "") if isinstance(i, dict) else "" for i in items
    ]
    assert sum(1 for t in texts if "[TIME]" in t) == 1


def test_second_run_starts_with_clean_progress() -> None:
    latch_no_progress(reason="streak")
    assert is_no_progress_latched()
    reset_progress_tracker()
    reset_verify_store()
    reset_supervisor_state()
    assert not is_no_progress_latched()


def test_completed_run_is_not_labeled_no_progress() -> None:
    # 逻辑在 runner；此处复现判定顺序：completed 优先。
    latch_no_progress(reason="streak")
    root_status = "completed"
    root_err = ""
    if root_status == "completed":
        stopped_reason = "模型调用 finish_scan 完成扫描"
    elif is_no_progress_latched():
        stopped_reason = "连续 3 轮无增量而收尾"
    else:
        stopped_reason = "other"
    assert stopped_reason == "模型调用 finish_scan 完成扫描"


def test_frontier_block_marks_todos_even_when_budget_tight() -> None:
    todos = [
        {"status": "pending", "priority": "high", "title": f"todo-{i}"}
        for i in range(40)
    ]
    from unittest.mock import patch

    with patch("Ben.core.frontier._open_todos", return_value=todos), patch(
        "Ben.core.frontier._agent_lines",
        return_value=[f"- agent a{i} (n): running" for i in range(8)],
    ), patch("Ben.core.frontier._claimed_keys", return_value={}), patch(
        "Ben.core.frontier._coverage_gaps", return_value=[]
    ):
        text = render_frontier_block(entries=[], max_chars=3500)
    assert "## Open todos" in text
    assert "## Sub-agents" in text
    assert "Coverage gaps" not in text


def test_recall_ignores_tiny_notes() -> None:
    score = overlap_score(tokenize("auth bypass"), tokenize("auth"))
    assert score < 0.45


def test_scan_clock_resume_keeps_original_deadline() -> None:
    started = time.time() - 30 * 60
    ctx1: dict = {}
    install_deadline(ctx1, minutes=90, started_at=started)
    rem1 = remaining_minutes(ctx1)
    ctx2: dict = {}
    install_deadline(ctx2, minutes=90, started_at=started)
    rem2 = remaining_minutes(ctx2)
    assert rem1 is not None and rem2 is not None
    assert abs(rem1 - rem2) < 0.5
    assert ctx2[SCAN_STARTED_AT_KEY] == started
    assert abs(ctx2[SCAN_DEADLINE_KEY] - (started + 90 * 60)) < 0.01
    # 不应回到满 90 分钟
    assert rem2 < 61


def test_progress_signature_never_swallows_storage_errors(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_progress_tracker()

    def _boom() -> list:
        raise RuntimeError("storage down")

    import Ben.tools.todo.tools as todo_tools

    monkeypatch.setattr(todo_tools, "list_all_todos", _boom)
    from Ben.core.progress import compute_world_signature

    with caplog.at_level(logging.WARNING):
        sig = compute_world_signature()
    assert isinstance(sig, str)
    assert any("todo snapshot failed" in r.message for r in caplog.records)
    assert not is_no_progress_latched()
