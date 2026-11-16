"""任务认领：同一 (surface, risk_area) 同时只有一个在飞 owner。

键规范化与 coverage 一致。闸门在 spawn；释放挂在 TERMINAL 状态。
``BEN_CLAIM=0``（默认）时 try_claim 旁路，保持旧行为。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_claims_lock = threading.RLock()
_claims: dict[str, dict[str, Any]] = {}
_claims_path: Path | None = None


def claims_enabled() -> bool:
    raw = os.environ.get("BEN_CLAIM", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def normalize_claim_key(surface: str, risk_area: str) -> tuple[str, str]:
    return surface.strip().lower(), risk_area.strip().lower()


def _key_str(surface: str, risk_area: str) -> str:
    s, r = normalize_claim_key(surface, risk_area)
    return f"{s}\x1f{r}"


def reset_task_claims() -> None:
    """测试用。"""
    global _claims_path  # noqa: PLW0603
    with _claims_lock:
        _claims.clear()
        _claims_path = None


def hydrate_task_claims_from_disk(state_dir: Path) -> None:
    global _claims_path  # noqa: PLW0603
    _claims_path = state_dir / "claims.json"
    with _claims_lock:
        _claims.clear()
        if not _claims_path.exists():
            return
        try:
            data = json.loads(_claims_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "claims.json at %s is unreadable; starting empty",
                _claims_path,
            )
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        for key, entry in data.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            expires_at = entry.get("expires_at")
            if isinstance(expires_at, (int, float)) and expires_at <= now:
                continue
            _claims[key] = entry
        logger.info(
            "task claims hydrated from %s (%d claim(s))",
            _claims_path,
            len(_claims),
        )


def _persist_locked() -> None:
    path = _claims_path
    if path is None:
        return
    payload = json.dumps(_claims, ensure_ascii=False, indent=2, default=str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)
    except OSError:
        logger.exception("claims persist failed at %s", path)


def _purge_expired_locked(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [
        key
        for key, entry in _claims.items()
        if isinstance(entry.get("expires_at"), (int, float))
        and float(entry["expires_at"]) <= now
    ]
    for key in expired:
        _claims.pop(key, None)


def try_claim(
    surface: str,
    risk_area: str,
    *,
    agent_id: str,
    agent_name: str | None = None,
    ttl_minutes: float | None = None,
) -> dict[str, Any]:
    """尝试认领。BEN_CLAIM 关闭时直接成功（不落盘）。"""
    if not surface.strip() or not risk_area.strip():
        return {
            "success": False,
            "error": "surface and risk_area are required to claim a work unit",
        }
    if not claims_enabled():
        return {
            "success": True,
            "bypassed": True,
            "surface": surface.strip(),
            "risk_area": risk_area.strip(),
            "agent_id": agent_id,
        }

    key = _key_str(surface, risk_area)
    norm_s, norm_r = normalize_claim_key(surface, risk_area)
    now = time.time()
    ttl = float(ttl_minutes) if ttl_minutes and ttl_minutes > 0 else 67.5
    expires_at = now + ttl * 60.0

    with _claims_lock:
        _purge_expired_locked(now)
        existing = _claims.get(key)
        if existing is not None:
            holder = str(existing.get("agent_id") or "")
            if holder and holder != agent_id:
                return {
                    "success": False,
                    "error": (
                        f"'{surface.strip()}' ({risk_area.strip()}) is already claimed by "
                        f"agent {holder}"
                        + (
                            f" ({existing.get('agent_name')})"
                            if existing.get("agent_name")
                            else ""
                        )
                        + ". send_message_to_agent that owner, or pick a different surface."
                    ),
                    "holder_agent_id": holder,
                    "holder_agent_name": existing.get("agent_name"),
                }
        entry = {
            "surface": surface.strip(),
            "risk_area": risk_area.strip(),
            "surface_key": norm_s,
            "risk_area_key": norm_r,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "claimed_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "claimed_at_epoch": now,
            "expires_at": expires_at,
            "ttl_minutes": ttl,
        }
        _claims[key] = entry
        _persist_locked()
    return {
        "success": True,
        "surface": surface.strip(),
        "risk_area": risk_area.strip(),
        "agent_id": agent_id,
        "expires_at": expires_at,
    }


def release_for_agent(agent_id: str) -> list[tuple[str, str]]:
    """释放该 agent 持有的全部认领；返回 (surface, risk_area) 列表。"""
    released: list[tuple[str, str]] = []
    with _claims_lock:
        to_drop = [
            key
            for key, entry in _claims.items()
            if str(entry.get("agent_id") or "") == agent_id
        ]
        for key in to_drop:
            entry = _claims.pop(key)
            released.append(
                (
                    str(entry.get("surface") or ""),
                    str(entry.get("risk_area") or ""),
                )
            )
        if to_drop:
            _persist_locked()
    return released


def owner_of(surface: str, risk_area: str) -> str | None:
    if not surface.strip() or not risk_area.strip():
        return None
    key = _key_str(surface, risk_area)
    with _claims_lock:
        _purge_expired_locked()
        entry = _claims.get(key)
        if entry is None:
            return None
        return str(entry.get("agent_id") or "") or None


def list_claims() -> list[dict[str, Any]]:
    with _claims_lock:
        _purge_expired_locked()
        return [dict(entry) for entry in _claims.values()]


def agent_holds_claim(agent_id: str, surface: str, risk_area: str) -> bool:
    holder = owner_of(surface, risk_area)
    return holder is not None and holder == agent_id
