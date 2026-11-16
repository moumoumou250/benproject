"""编排基线埋点：重复面 / root 工具分布 / stall。

只读计数，不改变调度行为。落盘 ``{run_dir}/orchestration_metrics.json``，
供校准 claim / no-progress / tier 的效果假设。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from Ben.core.jsonstore import atomic_write_json


logger = logging.getLogger(__name__)

_lock = threading.RLock()
_path: Path | None = None
_metrics: dict[str, Any] = {
    "coverage_duplicate_rejects": 0,
    "coverage_records_ok": 0,
    "root_tool_calls": {},
    "stall_notices": 0,
    "idle_resume_caps": 0,
    "claim_rejects": 0,
    "claim_ok": 0,
    "no_progress_injections": 0,
}


def reset_orchestration_metrics() -> None:
    """测试用：清空计数。"""
    global _path  # noqa: PLW0603
    with _lock:
        _path = None
        _metrics.clear()
        _metrics.update(
            {
                "coverage_duplicate_rejects": 0,
                "coverage_records_ok": 0,
                "root_tool_calls": {},
                "stall_notices": 0,
                "idle_resume_caps": 0,
                "claim_rejects": 0,
                "claim_ok": 0,
                "no_progress_injections": 0,
            }
        )


def bind_orchestration_metrics(run_dir: Path) -> None:
    """绑定本次 run 的落盘路径并立刻写一版空计数。"""
    global _path  # noqa: PLW0603
    run_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        _path = run_dir / "orchestration_metrics.json"
        _persist_locked()


def snapshot_orchestration_metrics() -> dict[str, Any]:
    with _lock:
        return json.loads(json.dumps(_metrics, ensure_ascii=False))


def record_coverage_duplicate() -> None:
    with _lock:
        _metrics["coverage_duplicate_rejects"] = (
            int(_metrics.get("coverage_duplicate_rejects", 0)) + 1
        )
        _persist_locked()


def record_coverage_ok() -> None:
    with _lock:
        _metrics["coverage_records_ok"] = int(_metrics.get("coverage_records_ok", 0)) + 1
        _persist_locked()


def record_root_tool_call(tool_name: str) -> None:
    name = (tool_name or "?").strip() or "?"
    with _lock:
        bucket = _metrics.setdefault("root_tool_calls", {})
        if not isinstance(bucket, dict):
            bucket = {}
            _metrics["root_tool_calls"] = bucket
        bucket[name] = int(bucket.get(name, 0)) + 1
        _persist_locked()


def record_stall_notice() -> None:
    with _lock:
        _metrics["stall_notices"] = int(_metrics.get("stall_notices", 0)) + 1
        _persist_locked()


def record_idle_resume_cap() -> None:
    with _lock:
        _metrics["idle_resume_caps"] = int(_metrics.get("idle_resume_caps", 0)) + 1
        _persist_locked()


def record_claim_reject() -> None:
    with _lock:
        _metrics["claim_rejects"] = int(_metrics.get("claim_rejects", 0)) + 1
        _persist_locked()


def record_claim_ok() -> None:
    with _lock:
        _metrics["claim_ok"] = int(_metrics.get("claim_ok", 0)) + 1
        _persist_locked()


def record_no_progress_injection() -> None:
    with _lock:
        _metrics["no_progress_injections"] = (
            int(_metrics.get("no_progress_injections", 0)) + 1
        )
        _persist_locked()


def coverage_duplicate_rate() -> float | None:
    """重复拒绝 / (成功 + 拒绝)；无样本时返回 None。"""
    with _lock:
        ok = int(_metrics.get("coverage_records_ok", 0))
        dup = int(_metrics.get("coverage_duplicate_rejects", 0))
    total = ok + dup
    if total <= 0:
        return None
    return dup / total


def _persist_locked() -> None:
    path = _path
    if path is None:
        return
    try:
        atomic_write_json(path, _metrics)
    except OSError:
        logger.exception("orchestration_metrics persist failed at %s", path)
