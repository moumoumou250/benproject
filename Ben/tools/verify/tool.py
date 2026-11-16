"""非 HTTP 发现的对抗性验证队列（fail-closed 的半边）。

``verify_finding`` 入队并（若有 spawner）以 ``tier=validator``、
``inherit_context=false`` 派生子 agent；**不**阻塞 ``finish_scan``。
HTTP 类继续走 reporting 的 exchange-id 硬门，本工具对已有
``http_request_id`` / exchange 证据的发现直接跳过。
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agents import RunContextWrapper, function_tool


logger = logging.getLogger(__name__)

VerifyStatus = Literal["queued", "upheld", "refuted", "insufficient", "skipped_http"]

_verify_lock = threading.RLock()
_verify_path: Path | None = None
_verify_store: dict[str, dict[str, Any]] = {}


def reset_verify_store() -> None:
    """测试用。"""
    global _verify_path  # noqa: PLW0603
    with _verify_lock:
        _verify_store.clear()
        _verify_path = None


def hydrate_verify_from_disk(state_dir: Path) -> None:
    global _verify_path  # noqa: PLW0603
    _verify_path = state_dir / "verify_queue.json"
    with _verify_lock:
        _verify_store.clear()
        if not _verify_path.exists():
            return
        try:
            data = json.loads(_verify_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("verify_queue.json unreadable at %s", _verify_path)
            return
        if isinstance(data, dict):
            _verify_store.update(
                {
                    str(k): v
                    for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, dict)
                }
            )


def _persist_locked() -> None:
    path = _verify_path
    if path is None:
        return
    payload = json.dumps(_verify_store, ensure_ascii=False, indent=2, default=str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)
    except OSError:
        logger.exception("verify_queue persist failed at %s", path)


def list_verify_results() -> list[dict[str, Any]]:
    with _verify_lock:
        return [dict(v) for v in _verify_store.values()]


def get_verify_result(finding_id: str) -> dict[str, Any] | None:
    with _verify_lock:
        entry = _verify_store.get(finding_id)
        return dict(entry) if entry else None


def record_verify_result(
    finding_id: str,
    *,
    status: VerifyStatus,
    reason: str = "",
    agent_id: str | None = None,
) -> dict[str, Any]:
    with _verify_lock:
        entry = {
            "finding_id": finding_id,
            "status": status,
            "reason": reason,
            "agent_id": agent_id,
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        prev = _verify_store.get(finding_id)
        if prev and prev.get("queued_at"):
            entry["queued_at"] = prev["queued_at"]
        _verify_store[finding_id] = entry
        _persist_locked()
        return dict(entry)


def _finding_looks_http(report: dict[str, Any]) -> bool:
    if report.get("http_request_id") or report.get("exchange_id"):
        return True
    meta = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
    if meta.get("http_request_id") or meta.get("exchange_id"):
        return True
    poc = report.get("poc") if isinstance(report.get("poc"), dict) else {}
    if poc.get("http_request_id") or poc.get("exchange_id"):
        return True
    return False


def _ctx(ctx: RunContextWrapper) -> dict[str, Any]:
    return ctx.context if isinstance(ctx.context, dict) else {}


@function_tool(timeout=60)
async def verify_finding(
    ctx: RunContextWrapper,
    finding_id: str,
    claim: str,
    evidence: str,
    falsifier: str,
) -> str:
    """Queue adversarial verification for a non-HTTP finding.

    Spawns a short-lived ``validator`` child (no inherited context) whose
    only job is to try to **falsify** the claim. HTTP findings that already
    carry exchange/request ids are skipped — the reporting tool's evidence
    gate already covers them. Results land in the verify queue; ``finish_scan``
    does **not** wait for them.

    Args:
        finding_id: Id from ``create_vulnerability_report`` / ``list_reports``.
        claim: One-sentence claim under test (what would be true if valid).
        evidence: Concrete evidence already collected (URLs, params, output).
        falsifier: What observation would **refute** the claim.
    """
    inner = _ctx(ctx)
    finding_id = (finding_id or "").strip()
    claim = (claim or "").strip()
    evidence = (evidence or "").strip()
    falsifier = (falsifier or "").strip()
    if not finding_id or not claim or not falsifier:
        return json.dumps(
            {
                "success": False,
                "error": "finding_id, claim, and falsifier are required",
            },
            ensure_ascii=False,
        )

    from Ben.report import get_global_report_state

    state = get_global_report_state()
    report = None
    if state is not None:
        report = next(
            (
                r
                for r in state.get_existing_vulnerabilities()
                if str(r.get("id")) == finding_id
            ),
            None,
        )
    if report is None:
        return json.dumps(
            {"success": False, "error": f"finding {finding_id!r} not found"},
            ensure_ascii=False,
        )
    if _finding_looks_http(report):
        entry = record_verify_result(
            finding_id,
            status="skipped_http",
            reason="HTTP exchange evidence gate already applies",
        )
        return json.dumps({"success": True, **entry}, ensure_ascii=False)

    queued_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    with _verify_lock:
        _verify_store[finding_id] = {
            "finding_id": finding_id,
            "status": "queued",
            "claim": claim,
            "evidence": evidence,
            "falsifier": falsifier,
            "queued_at": queued_at,
            "updated_at": queued_at,
        }
        _persist_locked()

    spawner = inner.get("spawn_child_agent")
    spawned: dict[str, Any] | None = None
    if callable(spawner):
        task = (
            f"Adversarial validation only.\n"
            f"Finding id: {finding_id}\n"
            f"Claim: {claim}\n"
            f"Evidence: {evidence}\n"
            f"Falsifier (what would refute it): {falsifier}\n\n"
            "Try to falsify the claim. Do not inherit hunter narrative. "
            "When done, call agent_finish with result_summary stating "
            "upheld | refuted | insufficient and why. "
            "Also call record_coverage if you ruled the surface out."
        )
        try:
            spawned = await spawner(
                parent_ctx=inner,
                name=f"Validator {finding_id}",
                task=task,
                skills=[],
                parent_history=[],
                minutes=10.0,
                surface=str(report.get("endpoint") or report.get("title") or finding_id),
                risk_area="validation",
                tier="validator",
            )
            if isinstance(spawned, dict) and spawned.get("agent_id"):
                with _verify_lock:
                    entry = _verify_store.get(finding_id, {})
                    entry["agent_id"] = spawned["agent_id"]
                    entry["updated_at"] = datetime.now(UTC).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    _verify_store[finding_id] = entry
                    _persist_locked()
        except Exception as exc:
            logger.exception("verify_finding spawn failed for %s", finding_id)
            return json.dumps(
                {
                    "success": True,
                    "finding_id": finding_id,
                    "status": "queued",
                    "spawn_error": str(exc),
                    "message": "Queued without child; finish_scan will treat as unverified",
                },
                ensure_ascii=False,
            )

    return json.dumps(
        {
            "success": True,
            "finding_id": finding_id,
            "status": "queued",
            "spawned": spawned,
            "message": (
                "Verification queued. finish_scan will not block on this; "
                "unfinished entries stay unverified."
            ),
        },
        ensure_ascii=False,
        default=str,
    )


@function_tool(timeout=30)
async def record_verify_outcome(
    ctx: RunContextWrapper,
    finding_id: str,
    status: str,
    reason: str = "",
) -> str:
    """Record the outcome of an adversarial verification (validator child).

    Args:
        finding_id: Finding under test.
        status: ``upheld`` / ``refuted`` / ``insufficient``.
        reason: Short justification.
    """
    inner = _ctx(ctx)
    me = inner.get("agent_id")
    normalized = (status or "").strip().lower()
    if normalized not in {"upheld", "refuted", "insufficient"}:
        return json.dumps(
            {
                "success": False,
                "error": "status must be upheld | refuted | insufficient",
            },
            ensure_ascii=False,
        )
    entry = record_verify_result(
        (finding_id or "").strip(),
        status=normalized,  # type: ignore[arg-type]
        reason=reason or "",
        agent_id=me if isinstance(me, str) else None,
    )
    return json.dumps({"success": True, **entry}, ensure_ascii=False)


def unfinished_verify_ids() -> list[str]:
    with _verify_lock:
        return [
            fid
            for fid, entry in _verify_store.items()
            if entry.get("status") == "queued"
        ]
