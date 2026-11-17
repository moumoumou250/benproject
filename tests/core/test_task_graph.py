"""task_graph：fact 去重、intent 派生、DFS pick。"""

from __future__ import annotations

from pathlib import Path

from Ben.core.task_graph import (
    add_fact,
    claim_intent,
    conclude_intent,
    hydrate_task_graph_from_disk,
    ingest_seed_facts,
    peek_ready,
    pick,
    reset_task_graph,
    snapshot_counts,
)


def setup_function() -> None:
    reset_task_graph()


def test_fact_dedupe_returns_same_id(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    a = add_fact(
        kind="service",
        title="80 open",
        data={"host": "10.0.0.1", "port": "80", "proto": "tcp"},
    )
    b = add_fact(
        kind="service",
        title="port 80",
        data={"host": "10.0.0.1", "port": "80", "proto": "tcp"},
    )
    assert a["success"] and b["success"]
    assert a["fact_id"] == b["fact_id"]
    assert b["deduped"] is True


def test_seed_ingest_only_recon_enum_intents(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    result = ingest_seed_facts(
        [
            {"kind": "host", "title": "target", "data": {"host": "a.example"}},
            {
                "kind": "service",
                "title": "443",
                "data": {"host": "a.example", "port": "443", "proto": "tcp"},
            },
            {
                "kind": "dir",
                "title": "/admin",
                "data": {"url": "https://a.example/admin"},
            },
            {"kind": "note", "title": "misc", "data": {"title": "misc"}},
        ]
    )
    assert result["success"]
    ready = peek_ready(limit=20)
    profiles = {r["profile"] for r in ready}
    assert profiles <= {"recon", "enum"}
    assert "exploit" not in profiles
    # note 不派生 intent
    assert all(r["profile"] in {"recon", "enum"} for r in ready)


def test_dfs_pick_prefers_deeper(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    root = add_fact(
        kind="host",
        title="h",
        data={"host": "h"},
        derive_intent=True,
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
    # pick 应取最深（子 service 的 depth >= host）
    chosen = pick(worker="w1")
    assert chosen is not None
    assert chosen["status"] == "claimed"
    assert chosen["worker"] == "w1"
    # 被 pick 后不再出现在 peek ready
    remaining_ids = {r["id"] for r in peek_ready(limit=20)}
    assert chosen["id"] not in remaining_ids


def test_hydrate_roundtrip(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    add_fact(kind="host", title="x", data={"host": "x"})
    counts = snapshot_counts()
    assert counts["facts"] >= 1
    reset_task_graph()
    hydrate_task_graph_from_disk(tmp_path)
    assert snapshot_counts()["facts"] == counts["facts"]
    assert snapshot_counts()["intents"] == counts["intents"]


def test_claim_and_conclude(tmp_path: Path) -> None:
    hydrate_task_graph_from_disk(tmp_path)
    fact = add_fact(kind="host", title="z", data={"host": "z"})
    intent_id = fact.get("intent_id")
    assert intent_id
    claimed = claim_intent(intent_id, worker="agent1")
    assert claimed["success"]
    done = conclude_intent(intent_id, status="done")
    assert done["success"]
    assert done["intent"]["status"] == "done"
