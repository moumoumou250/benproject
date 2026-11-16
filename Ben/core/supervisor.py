"""心跳落盘 + 看护：心跳停更且无在飞 LLM 时优雅收尾。

``run_dir/heartbeat.jsonl`` 每轮一行。supervisor task 由 runner 启动；
误重启阈值：``stream_total_timeout × 2`` 且无 ACTIVE 子 agent / 无进行中的
root LLM 调用标志时才触发。

已知限制：单进程单 run 假设；``mark_llm_*`` 用带时间戳的登记表，
陈旧在飞会在 ``stale_after_s`` 后被忽略（模型异常不调 on_llm_end 时的兜底）。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from Ben.core.agents import AgentCoordinator


logger = logging.getLogger(__name__)

_heartbeat_path: Path | None = None
# None = 尚未写过心跳；可用负值（测试把 age 设得很久以前时）。
_last_heartbeat_mono: float | None = None
# key → start monotonic；避免 set[float] 同毫秒碰撞。
_llm_starts: dict[int, float] = {}
_llm_start_seq = itertools.count(1)

# graceful 后等待 tool 收尾的秒数，再升级 immediate / task.cancel。
DEFAULT_STALL_GRACE_S = 60.0


def reset_supervisor_state(*, clear_heartbeat_path: bool = True) -> None:
    """清空看护状态。生产路径在每次 run 开头调用。"""
    global _heartbeat_path, _last_heartbeat_mono  # noqa: PLW0603
    _llm_starts.clear()
    _last_heartbeat_mono = None
    if clear_heartbeat_path:
        _heartbeat_path = None


def clear_llm_inflight() -> None:
    """只清在飞计数（保留 heartbeat path），供 run_agent_loop finally 使用。"""
    _llm_starts.clear()


def bind_heartbeat(run_dir: Path) -> Path:
    global _heartbeat_path, _last_heartbeat_mono  # noqa: PLW0603
    run_dir.mkdir(parents=True, exist_ok=True)
    _heartbeat_path = run_dir / "heartbeat.jsonl"
    _last_heartbeat_mono = time.monotonic()
    return _heartbeat_path


def mark_llm_start() -> int:
    """登记一次 LLM 调用开始；返回登记 key（测试用）。"""
    key = next(_llm_start_seq)
    _llm_starts[key] = time.monotonic()
    return key


def mark_llm_end() -> None:
    """结束最早的一次在飞登记。"""
    if not _llm_starts:
        return
    oldest = min(_llm_starts, key=_llm_starts.get)  # type: ignore[arg-type]
    _llm_starts.pop(oldest, None)


def llm_in_flight(*, stale_after_s: float | None = None) -> int:
    """在飞 LLM 数；``stale_after_s`` 内未配对 end 的条目会被丢弃。"""
    if stale_after_s is not None and stale_after_s >= 0:
        now = time.monotonic()
        for key, started in list(_llm_starts.items()):
            if now - started > stale_after_s:
                _llm_starts.pop(key, None)
    return len(_llm_starts)


def write_heartbeat(row: dict[str, Any]) -> None:
    global _last_heartbeat_mono  # noqa: PLW0603
    from Ben.core.jsonstore import append_jsonl

    path = _heartbeat_path
    payload = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        **row,
    }
    if path is not None:
        try:
            append_jsonl(path, payload)
        except OSError:
            logger.exception("heartbeat write failed at %s", path)
    _last_heartbeat_mono = time.monotonic()


def last_heartbeat_age_s() -> float:
    if _last_heartbeat_mono is None:
        return 0.0
    return time.monotonic() - _last_heartbeat_mono


def _force_set_last_heartbeat_age_for_tests(age_s: float) -> None:
    """测试用：把上次心跳设成 ``age_s`` 秒前（允许单调时钟为负）。"""
    global _last_heartbeat_mono  # noqa: PLW0603
    _last_heartbeat_mono = time.monotonic() - age_s


async def run_supervisor(
    *,
    coordinator: AgentCoordinator,
    root_id: str,
    stall_timeout_s: float,
    poll_s: float = 15.0,
    stall_grace_s: float = DEFAULT_STALL_GRACE_S,
) -> None:
    """后台看护：心跳超时且无在飞 LLM / 无 ACTIVE 子 agent → 先礼后兵停 root。

    不自动 ``-r`` 重启（那是 CLI 职责）；只负责卡死收尾。
    合法空闲（等用户 / 等子 agent / 人工或预算暂停）不算卡死。
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
        # 合法空闲：等用户、等子 agent、被暂停，都不算卡死。
        wait_kind = coordinator.wait_kinds.get(root_id)
        if wait_kind in ("user", "agents"):
            continue
        if coordinator.operator_paused or coordinator.budget_paused:
            continue
        if llm_in_flight(stale_after_s=stall_timeout_s) > 0:
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

            latch_no_progress(reason="heartbeat")
        except Exception:  # noqa: BLE001
            logger.debug("supervisor: latch_no_progress failed", exc_info=True)
        try:
            stopped = await coordinator.cancel_descendants_graceful(root_id)
            if stall_grace_s > 0:
                await asyncio.sleep(stall_grace_s)
            for aid in stopped:
                runtime = coordinator.runtimes.get(aid)
                stream = getattr(runtime, "stream", None) if runtime is not None else None
                if stream is not None:
                    try:
                        stream.cancel(mode="immediate")
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "supervisor: immediate cancel failed for %s",
                            aid,
                            exc_info=True,
                        )
            await coordinator.cancel_descendants(root_id)
            await coordinator.set_status(
                root_id,
                "stopped",
                error="supervisor: heartbeat stalled",
            )
        except Exception:  # noqa: BLE001
            logger.exception("supervisor stop failed")
        return
