"""#5 resume 已测面 + #4 frontier 注入 / 签名节流。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from Ben.core.frontier import (
    FRONTIER_MARKER,
    MAX_FRONTIER_CHARS,
    clear_frontier_signatures,
    frontier_content_signature,
    inject_or_replace_frontier,
    merge_resume_instruction,
    render_excluded_coverage_brief,
    render_frontier_block,
)


@pytest.fixture(autouse=True)
def _reset_frontier_sigs() -> None:
    clear_frontier_signatures()


def test_render_excluded_coverage_brief_lists_closed_rows() -> None:
    entries = [
        {
            "surface": "/api/users",
            "risk_area": "idor",
            "outcome": "ruled_out",
            "evidence": "authz check present",
        },
        {
            "surface": "/health",
            "risk_area": "info leak",
            "outcome": "no_issue_found",
        },
        {
            "surface": "/legacy",
            "risk_area": "sqli",
            "outcome": "not_applicable",
        },
        {
            "surface": "/admin",
            "risk_area": "xss",
            "outcome": "needs_follow_up",
        },
        {
            "surface": "/pay",
            "risk_area": "idor",
            "outcome": "reported",
        },
    ]
    brief = render_excluded_coverage_brief(entries)
    assert "## Previously excluded surfaces (resume)" in brief
    assert "ruled_out" in brief and "/api/users" in brief
    assert "no_issue_found" in brief and "/health" in brief
    assert "not_applicable" in brief
    assert "/admin" not in brief  # open — not excluded
    assert "/pay" not in brief  # reported — not excluded
    assert "update_coverage" in brief
    assert "New evidence may reopen" in brief


def test_render_excluded_empty_when_nothing_closed() -> None:
    assert render_excluded_coverage_brief([]) == ""
    assert (
        render_excluded_coverage_brief(
            [{"surface": "x", "risk_area": "y", "outcome": "needs_follow_up"}]
        )
        == ""
    )


def test_merge_resume_instruction_combines_and_allows_empty_user() -> None:
    brief = render_excluded_coverage_brief(
        [{"surface": "/a", "risk_area": "idor", "outcome": "ruled_out"}]
    )
    assert brief
    merged = merge_resume_instruction("Focus on checkout.", excluded_brief=brief)
    assert merged.startswith("## Previously excluded")
    assert "Focus on checkout." in merged
    only_brief = merge_resume_instruction("", excluded_brief=brief)
    assert only_brief == brief
    only_user = merge_resume_instruction("hello", excluded_brief="")
    assert only_user == "hello"
    assert merge_resume_instruction("", excluded_brief="") == ""


def test_render_frontier_block_includes_gaps_and_marker() -> None:
    entries = [
        {
            "surface": "/checkout",
            "risk_area": "idor",
            "outcome": "needs_follow_up",
            "evidence": "role pivot unverified",
        },
        {
            "surface": "/login",
            "risk_area": "xss",
            "outcome": "ruled_out",
        },
    ]
    block = render_frontier_block(coordinator=None, entries=entries)
    assert block.startswith(FRONTIER_MARKER)
    assert "do not probe the target yourself" in block
    assert "/checkout" in block
    assert "needs_follow_up" in block or "Coverage gaps" in block or "## Ready" in block
    assert "excluded=1" in block
    assert "## Ready" in block
    assert "## Claimed" in block
    assert "suggest:" in block
    assert len(block) <= MAX_FRONTIER_CHARS


def test_inject_or_replace_frontier_throttles_and_replaces() -> None:
    items: list[dict[str, Any]] = [{"role": "user", "content": "hello"}]
    block_a = (
        f"{FRONTIER_MARKER}\n"
        "Orchestration snapshot only — do not probe the target yourself.\n"
        "## Coverage gaps (needs_follow_up)\n"
        "- /a · idor\n"
        "## Coverage counts: total=1 open=1 excluded=0 reported=0\n"
        "## Open todos\n"
        "- (none)\n"
        "## Sub-agents\n"
        "- (none)"
    )
    assert inject_or_replace_frontier(items, block_a, agent_id="root") is True
    assert sum(1 for i in items if FRONTIER_MARKER in str(i.get("content"))) == 1

    # 同签名 → 跳过
    assert inject_or_replace_frontier(items, block_a, agent_id="root") is False
    assert sum(1 for i in items if FRONTIER_MARKER in str(i.get("content"))) == 1

    block_b = block_a.replace("/a · idor", "/b · xss")
    assert frontier_content_signature(block_a) != frontier_content_signature(block_b)
    assert inject_or_replace_frontier(items, block_b, agent_id="root") is True
    # 替换而非追加
    assert sum(1 for i in items if FRONTIER_MARKER in str(i.get("content"))) == 1
    assert "/b · xss" in items[-1]["content"]


@pytest.mark.asyncio
async def test_hooks_inject_frontier_only_for_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from Ben.core.hooks import ReportUsageHooks

    hooks = ReportUsageHooks(model="test-model", max_budget_usd=None)
    root_ctx = MagicMock()
    root_ctx.context = {"agent_id": "root", "parent_id": None}
    root_ctx.usage = MagicMock(requests=0)
    child_ctx = MagicMock()
    child_ctx.context = {"agent_id": "child1", "parent_id": "root"}
    child_ctx.usage = MagicMock(requests=0)

    root_items: list[dict[str, Any]] = []
    child_items: list[dict[str, Any]] = []

    stub = (
        f"{FRONTIER_MARKER}\n"
        "Orchestration snapshot only — do not probe the target yourself.\n"
        "## Coverage gaps (needs_follow_up)\n- (none)\n"
        "## Coverage counts: total=0 open=0 excluded=0 reported=0\n"
        "## Open todos\n- (none)\n"
        "## Sub-agents\n- (none)"
    )
    monkeypatch.setattr(
        "Ben.core.frontier.render_frontier_block",
        lambda **_k: stub,
    )
    await hooks.on_llm_start(root_ctx, MagicMock(), None, root_items)
    await hooks.on_llm_start(child_ctx, MagicMock(), None, child_items)

    assert any(FRONTIER_MARKER in str(i.get("content")) for i in root_items)
    assert not any(FRONTIER_MARKER in str(i.get("content")) for i in child_items)

    # 第二轮同签名不重复追加
    before = len(root_items)
    await hooks.on_llm_start(root_ctx, MagicMock(), None, root_items)
    assert len(root_items) == before
