"""Run 级无进展检测：内容签名，不用目录 mtime / 纯长度。

签名覆盖 coverage(id,outcome)、vuln ids、todo done 计数、agent(id,status)、
claims(key,owner)。有在飞子 agent 时不计 stall。
``BEN_NO_PROGRESS_TURNS`` 默认 3；``0`` 关闭。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from Ben.core.agents import AgentCoordinator


logger = logging.getLogger(__name__)

_last_sig: str | None = None
_stall_streak: int = 0
_no_progress_latched: bool = False


def reset_progress_tracker() -> None:
    """测试用。"""
    global _last_sig, _stall_streak, _no_progress_latched  # noqa: PLW0603
    _last_sig = None
    _stall_streak = 0
    _no_progress_latched = False


def no_progress_threshold() -> int:
    raw = os.environ.get("BEN_NO_PROGRESS_TURNS", "3").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        logger.warning("BEN_NO_PROGRESS_TURNS=%r invalid; treating as 0 (off)", raw)
        return 0
    return max(0, value)


def is_no_progress_latched() -> bool:
    return _no_progress_latched


def latch_no_progress() -> None:
    global _no_progress_latched  # noqa: PLW0603
    _no_progress_latched = True


def stall_streak() -> int:
    return _stall_streak


def compute_world_signature(
    *,
    coordinator: AgentCoordinator | None = None,
) -> str:
    """账本内容签名（稳定排序后 SHA-256 前 16 位）。"""
    coverage_parts: list[tuple[str, str]] = []
    try:
        from Ben.tools.coverage.tools import get_coverage_entries

        for entry in get_coverage_entries():
            eid = str(entry.get("entry_id") or entry.get("id") or "")
            if not eid:
                # get_coverage_entries 可能不带 id；用 surface+risk 兜底
                eid = (
                    f"{entry.get('surface', '')}|{entry.get('risk_area', '')}"
                )
            coverage_parts.append((eid, str(entry.get("outcome") or "")))
    except Exception:  # noqa: BLE001
        logger.debug("progress: coverage snapshot failed", exc_info=True)

    vuln_ids: list[str] = []
    try:
        from Ben.report import get_global_report_state

        state = get_global_report_state()
        if state is not None:
            for report in state.get_existing_vulnerabilities() or []:
                rid = report.get("id")
                if rid is not None:
                    vuln_ids.append(str(rid))
    except Exception:  # noqa: BLE001
        logger.debug("progress: vuln snapshot failed", exc_info=True)

    done_todos = 0
    try:
        from Ben.tools.todo import tools as todo_tools

        storage = getattr(todo_tools, "_todos_storage", {})
        if isinstance(storage, dict):
            for by_id in storage.values():
                if not isinstance(by_id, dict):
                    continue
                for todo in by_id.values():
                    if isinstance(todo, dict) and todo.get("status") == "done":
                        done_todos += 1
    except Exception:  # noqa: BLE001
        logger.debug("progress: todo snapshot failed", exc_info=True)

    agent_parts: list[tuple[str, str]] = []
    if coordinator is not None:
        for aid, status in sorted(coordinator.statuses.items()):
            agent_parts.append((aid, str(status)))

    claim_parts: list[tuple[str, str, str]] = []
    try:
        from Ben.core.task_claims import list_claims

        for claim in list_claims():
            claim_parts.append(
                (
                    str(claim.get("surface_key") or claim.get("surface") or ""),
                    str(claim.get("risk_area_key") or claim.get("risk_area") or ""),
                    str(claim.get("agent_id") or ""),
                )
            )
    except Exception:  # noqa: BLE001
        logger.debug("progress: claims snapshot failed", exc_info=True)

    payload = {
        "coverage": sorted(coverage_parts),
        "vulns": sorted(vuln_ids),
        "done_todos": done_todos,
        "agents": sorted(agent_parts),
        "claims": sorted(claim_parts),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def has_active_children(coordinator: AgentCoordinator | None) -> bool:
    if coordinator is None:
        return False
    from Ben.core.agents import ACTIVE_STATUSES

    return any(
        parent is not None and coordinator.statuses.get(aid) in ACTIVE_STATUSES
        for aid, parent in coordinator.parent_of.items()
    )


def observe_progress(
    *,
    coordinator: AgentCoordinator | None,
) -> dict[str, Any]:
    """每轮调用：更新 stall_streak；返回是否应注入 NO-PROGRESS。

    有在飞子 agent 时不计 stall（streak 保持、不递增）。
    """
    global _last_sig, _stall_streak  # noqa: PLW0603

    threshold = no_progress_threshold()
    sig = compute_world_signature(coordinator=coordinator)

    if threshold <= 0:
        _last_sig = sig
        _stall_streak = 0
        return {
            "signature": sig,
            "stall_streak": 0,
            "should_wrap_up": False,
            "enabled": False,
        }

    if has_active_children(coordinator):
        _last_sig = sig
        return {
            "signature": sig,
            "stall_streak": _stall_streak,
            "should_wrap_up": False,
            "enabled": True,
            "deferred_active_children": True,
        }

    if _last_sig is None:
        _last_sig = sig
        _stall_streak = 0
    elif sig == _last_sig:
        _stall_streak += 1
    else:
        _last_sig = sig
        _stall_streak = 0

    should = _stall_streak >= threshold
    if should:
        latch_no_progress()
    return {
        "signature": sig,
        "stall_streak": _stall_streak,
        "should_wrap_up": should,
        "enabled": True,
        "threshold": threshold,
    }
