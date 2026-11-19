"""把一个 run 的完整 agent 对话从 agents.db 导出成可读的 JSONL。

agents.db 里已经存着每个 agent 的完整会话条目（发给模型的 user/assistant/
function_call/function_call_output），但它是 SQLite，不便直接翻看。这个脚本
只读不写库，按 agent 各导出一个 ``.jsonl``（一行一条，保持原始顺序），并写
一份 ``index.json`` 说明谁是谁、各有多少条。

用法::

    python scripts/dump_transcript.py                     # 最近一次 run
    python scripts/dump_transcript.py ben_20261119_1015   # 指定 run 名
    python scripts/dump_transcript.py --run /path/to/run  # 指定目录
    python scripts/dump_transcript.py --out /tmp/tx       # 指定输出目录

默认输出到 ``_artifacts/transcripts/<run>/``，与 runs 分开，避免污染 resume
所依赖的 ``.state/``。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Ben.core.paths import latest_run_dir, resolve_run_dir, runs_base_dir  # noqa: E402


def resolve_run(arg: str | None) -> Path:
    if arg:
        candidate = Path(arg).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        found = resolve_run_dir(arg)
        if found is None:
            raise SystemExit(f"找不到 run：{arg}")
        return found
    latest = latest_run_dir()
    if latest is None:
        raise SystemExit(f"{runs_base_dir()} 下还没有任何 run")
    return latest


def read_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    """agents.json 里的图快照：id -> {name, parent_id, status}。"""
    path = run_dir / ".state" / "agents.json"
    if not path.is_file():
        return {}
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    statuses = snap.get("statuses") or {}
    parents = snap.get("parent_of") or {}
    names = snap.get("names") or {}
    return {
        aid: {
            "name": names.get(aid, aid),
            "parent_id": parents.get(aid),
            "status": statuses.get(aid),
        }
        for aid in statuses
    }


def _rows(db: Path) -> dict[str, list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "select session_id, message_data from agent_messages order by id"
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for session_id, data in cur:
            try:
                item = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                out.setdefault(str(session_id), []).append(item)
        return out
    finally:
        conn.close()


def _kind(item: dict[str, Any]) -> str:
    return str(item.get("type") or item.get("role") or "unknown")


def _preview(item: dict[str, Any], limit: int = 80) -> str:
    for key in ("content", "output", "arguments", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            flat = " ".join(value.split())
            return flat[:limit] + ("…" if len(flat) > limit else "")
    return ""


def _safe(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in name]
    return "".join(keep).strip("_") or "agent"


def dump(run_dir: Path, out_dir: Path) -> int:
    db = run_dir / ".state" / "agents.db"
    if not db.is_file():
        raise SystemExit(f"缺少会话库：{db}（这个 run 可能没跑起来）")

    sessions = _rows(db)
    meta = read_index(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    index: dict[str, Any] = {
        "run": run_dir.name,
        "run_dir": str(run_dir),
        "source": str(db),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "agents": [],
    }
    for aid, items in sessions.items():
        info = meta.get(aid, {})
        name = _safe(str(info.get("name") or aid))
        target = out_dir / f"{name}__{aid[:8]}.jsonl"
        with target.open("w", encoding="utf-8") as fh:
            for seq, item in enumerate(items, start=1):
                record = {"seq": seq, "agent_id": aid, "kind": _kind(item), **item}
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        index["agents"].append(
            {
                "agent_id": aid,
                "name": info.get("name", aid),
                "parent_id": info.get("parent_id"),
                "status": info.get("status"),
                "items": len(items),
                "file": target.name,
                "kinds": _counts(items),
                "last": _preview(items[-1]) if items else "",
            }
        )
        print(f"{target.name}  {len(items):>5} 条")

    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n共 {len(sessions)} 个 agent → {out_dir}")
    return 0


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = _kind(item)
        counts[key] = counts.get(key, 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", help="run 名；省略则取最近一次")
    parser.add_argument("--run", dest="run_opt", help="run 名或 run 目录")
    parser.add_argument("--out", help="输出目录（默认 _artifacts/transcripts/<run>）")
    args = parser.parse_args(argv)

    run_dir = resolve_run(args.run_opt or args.run)
    out_dir = (
        Path(args.out).expanduser()
        if args.out
        else runs_base_dir().parent / "transcripts" / run_dir.name
    )
    return dump(run_dir, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
