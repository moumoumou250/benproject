"""心跳落盘 + 看护：心跳停更且无在飞 LLM 时优雅收尾。

``run_dir/heartbeat.jsonl`` 每轮一行。supervisor task 由 runner 启动；
误重启阈值：``stream_total_timeout × 2`` 且无 ACTIVE 子 agent / 无进行中的
root LLM 调用标志时才触发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from Ben.core.agents import AgentCoordinator


logger = logging.getLogger(__name__)

_heartbeat_path: Path | None = None
_last_heartbeat_mono: float = 0.0
_llm_in_flight: int = 0


def reset_supervisor_state() -> None:
    """测试用。"""
    global _heartbeat_path, _last_heartbeat_mono, _llm_in_flight  # noqa: PLW0603
    _heartbeat_path = None
    _last_heartbeat_mono = 0.0
    _llm_in_flight = 0


def bind_heartbeat(run_dir: Path) -> Path:
    global _heartbeat_path, _last_heartbeat_mono  # noqa: PLW0603
    run_dir.mkdir(parents=True, exist_ok=True)
    _heartbeat_path = run_dir / "heartbeat.jsonl"
    _last_heartbeat_mono = time.monotonic()
    return _heartbeat_path


def mark_llm_start() -> None:
    global _llm_in_flight  # noqa: PLW0603
    _llm_in_flight += 1


def mark_llm_end() -> None:
    global _llm_in_flight  # noqa: PLW0603
    _llm_in_flight = max(0, _llm_in_flight - 1)


def llm_in_flight() -> int:
    return _llm_in_flight


def write_heartbeat(row: dict[str, Any]) -> None:
    global _last_heartbeat_mono  # noqa: PLW0603
    path = _heartbeat_path
    payload = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        **row,
    }
    line = json.dumps(payload, ensure_ascii=False, default=str)
    if path is not None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            logger.exception("heartbeat write failed at %s", path)
    _last_heartbeat_mono = time.monotonic()


def last_heartbeat_age_s() -> float:
    if _last_heartbeat_mono <= 0:
        return 0.0
    return time.monotonic() - _last_heartbeat_mono


async def run_supervisor(
    *,
    coordinator: AgentCoordinator,
    root_id: str,
    stall_timeout_s: float,
    poll_s: float = 15.0,
) -> None:
    """后台看护：心跳超时且无在飞 LLM / 无 ACTIVE 子 agent → 优雅停 root。

    不自动 ``-r`` 重启（那是 CLI 职责）；只负责卡死收尾。
    """
    from Ben.core.agents import ACTIVE_STATUSES, TERMINAL_STATUSES

    if stall_timeout_s <= 0:
        return
    while True:
        await asyncio.sleep(poll_s)
        if coordinator.is_shutting_down:
            return
        status = coordinator.statuses.get(root_id)
        if status in TERMINAL_STATUSES:
            return
        if llm_in_flight() > 0:
            continue
        has_active_child = any(
            parent is not None and coordinator.statuses.get(aid) in ACTIVE_STATUSES
            for aid, parent in coordinator.parent_of.items()
        )
        if has_active_child:
            continue
        age = last_heartbeat_age_s()
        if age < stall_timeout_s:
            continue
        logger.warning(
            "supervisor: heartbeat stale for %.0fs (limit %.0fs); "
            "gracefully stopping root %s",
            age,
            stall_timeout_s,
            root_id,
        )
        try:
            from Ben.core.progress import latch_no_progress

            latch_no_progress()
        except Exception:  # noqa: BLE001
            logger.debug("supervisor: latch_no_progress failed", exc_info=True)
        try:
            await coordinator.cancel_descendants_graceful(root_id)
            await coordinator.set_status(
                root_id,
                "stopped",
                error="supervisor: heartbeat stalled",
            )
        except Exception:  # noqa: BLE001
            logger.exception("supervisor stop failed")
        return
