"""同层老优先 / breadth-first 调度（P0.5）。"""

from __future__ import annotations

from pathlib import Path

from Ben.core.task_graph import (
    add_fact,
    hydrate_task_graph_from_disk,
    peek_ready,
    pick,
    reset_task_graph,
)


def setup_function() -> None:
    reset_task_graph()


def test_same_depth_oldest_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BEN_BREADTH_FIRST", raising=False)
    hydrate_task_graph_from_disk(tmp_path)
    first = add_fact(kind="host", title="a", data={"host": "a"}, derive_intent=True)
    second = add_fact(kind="host", title="b", data={"host": "b"}, derive_intent=True)
    assert first["success"] and second["success"]
    ready = peek_ready(limit=10)
    assert len(ready) >= 2
    # 深度全 0 → FIFO：先看到的 a 应在前
    assert ready[0]["id"] == first["intent_id"]
    chosen = pick(worker="w")
    assert chosen is not None
    assert chosen["id"] == first["intent_id"]


def test_breadth_first_prefers_shallow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BEN_BREADTH_FIRST", "1")
    hydrate_task_graph_from_disk(tmp_path)
    root = add_fact(
        kind="host", title="h", data={"host": "h"}, derive_intent=True
    )
    child = add_fact(
        kind="service",
        title="s",
        data={"host": "h", "port": "22", "proto": "tcp"},
        parent_fact=root["fact_id"],
        derive_intent=True,
    )
    assert root["success"] and child["success"]
    ready = peek_ready(limit=10)
    assert ready
    # 广度：浅层 host intent 优先于更深的 service
    assert ready[0]["id"] == root["intent_id"]
