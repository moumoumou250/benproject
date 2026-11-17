"""frontier：DFS 非空时折叠 Ready。"""

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
            "surface": "/admin",
            "risk_area": "xss",
            "outcome": "needs_follow_up",
            "evidence": "need check",
        }
    ]
    block = render_frontier_block(coordinator=None, entries=entries)
    assert "## Next (DFS order)" in block
    assert "- (none)" in block or "Next (DFS" in block
    assert "/admin" in block
    assert "deferred" not in block


def test_ready_collapsed_when_dfs_nonempty(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    add_fact(kind="host", title="t", data={"host": "t"})
    entries = [
        {
            "surface": "/admin",
            "risk_area": "xss",
            "outcome": "needs_follow_up",
            "evidence": "need check",
        }
    ]
    block = render_frontier_block(coordinator=None, entries=entries)
    assert "## Next (DFS order)" in block
    assert "profile=recon" in block or "recon" in block
    assert "deferred" in block
    # Ready 不应再展开具体 gap + suggest
    assert "suggest: name=" not in block.split("## Ready")[1].split("##")[0]
    # DFS 段应在 Ready 之前
    assert block.index("## Next (DFS order)") < block.index("## Ready")
