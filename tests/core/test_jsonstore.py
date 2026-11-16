"""M11：atomic JSON + fsync。"""

from __future__ import annotations

import json
from pathlib import Path

from Ben.core.jsonstore import append_jsonl, atomic_write_json


def test_jsonstore_atomic_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"
    atomic_write_json(path, {"a": 1, "b": [2, 3]})
    assert path.stat().st_size > 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"a": 1, "b": [2, 3]}

    log = tmp_path / "heartbeat.jsonl"
    append_jsonl(log, {"ts": 1})
    append_jsonl(log, {"ts": 2})
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["ts"] == 2
