"""frontier：DFS 与 Ready 并排，不再因 DFS 非空折叠 Ready（P0.5）。"""

from __future__ import annotations

from pathlib import Path

from Ben.core.frontier import clear_frontier_signatures, render_frontier_block
from Ben.core.task_graph import add_fact, hydrate_task_graph_from_disk, reset_task_graph


def setup_function() -> None:
    reset_task_graph()
    clear_frontier_signatures()


def test_ready_expanded_when_no_dfs(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    entries = [
        {
            "entry_id": "abc123",
            "surface": "/admin",
            "risk_area": "xss",
            "outcome": "needs_follow_up",
            "evidence": "need check",
        }
    ]
    block = render_frontier_block(coordinator=None, entries=entries)
    assert "## Next (DFS order)" in block
    assert "/admin" in block
    assert "deferred" not in block
    assert "coverage_entry_id=" in block


def test_ready_stays_expanded_when_dfs_nonempty(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    add_fact(kind="host", title="t", data={"host": "t"})
    entries = [
        {
            "entry_id": "abc123",
            "surface": "/admin",
            "risk_area": "xss",
            "outcome": "pending",
            "evidence": "inventory",
        }
    ]
    block = render_frontier_block(coordinator=None, entries=entries)
    assert "## Next (DFS order)" in block
    assert "profile=recon" in block or "recon" in block
    assert "deferred" not in block
    ready_section = block.split("## Ready")[1].split("##")[0]
    assert "/admin" in ready_section
    assert "coverage_entry_id=" in ready_section
    assert block.index("## Next (DFS order)") < block.index("## Ready")
