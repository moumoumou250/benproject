"""对抗性验证队列：入队 → validator 结算 → finish_scan 消费。

HTTP 发现（带 ``http_exchange_ids``）走 reporting 硬门，本工具跳过。
非 HTTP 发现由 root 调 ``verify_finding`` 入队；``record_verify_outcome``
仅分配到的 validator（或未派单时的结算方）可写。``finish_scan`` 读取
队列，给每条发现打 ``verification_status``，并把 refuted 移出正文。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agents import RunContextWrapper, function_tool

from Ben.core.agents import coordinator_from_context
from Ben.core.jsonstore import atomic_write_json


logger = logging.getLogger(__name__)

VerifyStatus = Literal["queued", "upheld", "refuted", "insufficient", "skipped_http"]

_verify_lock = threading.RLock()
_verify_path: Path | None = None
_verify_store: dict[str, dict[str, Any]] = {}


def reset_verify_store() -> None:
    """测试 / 每 run 开头调用。"""
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
    try:
        atomic_write_json(path, _verify_store)
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
        if prev and prev.get("claim") and "claim" not in entry:
            entry["claim"] = prev.get("claim")
        _verify_store[finding_id] = entry
        _persist_locked()
        return dict(entry)


def _finding_looks_http(report: dict[str, Any]) -> bool:
    # H8：报告字段是 http_exchange_ids；旧字段保留兼容。
    for holder in (report, report.get("metadata"), report.get("poc")):
        if not isinstance(holder, dict):
            continue
        ids = holder.get("http_exchange_ids")
        if isinstance(ids, list) and ids:
            return True
        if holder.get("http_request_id") or holder.get("exchange_id"):
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

    Root-only. Spawns a short-lived ``validator`` child (no inherited
    context) whose job is to try to **falsify** the claim. HTTP findings
    that already carry ``http_exchange_ids`` are skipped. Results land in
    the verify queue; ``finish_scan`` consumes them and annotates each
    finding with ``verification_status``.

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

    # M7：幂等 — queued/upheld/refuted 不重复派单。
    existing = get_verify_result(finding_id)
    if existing and existing.get("status") in {"queued", "upheld", "refuted"}:
        return json.dumps(
            {"success": True, "already": True, **existing},
            ensure_ascii=False,
            default=str,
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
            "When done, call record_verify_outcome with upheld | refuted | "
            "insufficient, then agent_finish. "
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
            # M6：spawn 失败必须诚实返回，不能假装已排队验证。
            logger.exception("verify_finding spawn failed for %s", finding_id)
            return json.dumps(
                {
                    "success": False,
                    "finding_id": finding_id,
                    "status": "not_spawned",
                    "spawn_error": str(exc),
                    "message": (
                        "Not queued for verification: no child slot or spawn "
                        "error. Either wait for a slot and retry, or accept "
                        "this finding as unverified."
                    ),
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
                "Verification queued. finish_scan annotates "
                "verification_status; unfinished entries stay unverified."
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
    finding_id = (finding_id or "").strip()
    normalized = (status or "").strip().lower()
    if normalized not in {"upheld", "refuted", "insufficient"}:
        return json.dumps(
            {
                "success": False,
                "error": "status must be upheld | refuted | insufficient",
            },
            ensure_ascii=False,
        )

    entry = get_verify_result(finding_id)
    if entry is None:
        return json.dumps(
            {
                "success": False,
                "error": "unknown finding under verification",
            },
            ensure_ascii=False,
        )

    # H6：仅分配到的 validator（或未派单时）可结算；禁止 hunter 自证。
    coordinator = coordinator_from_context(inner)
    tier = None
    if coordinator is not None and isinstance(me, str):
        meta = coordinator.metadata.get(me) or {}
        if isinstance(meta, dict):
            tier = meta.get("tier")
    owner = entry.get("agent_id")
    if owner and me != owner and tier != "validator":
        return json.dumps(
            {
                "success": False,
                "error": "only the assigned validator may record this outcome",
            },
            ensure_ascii=False,
        )

    saved = record_verify_result(
        finding_id,
        status=normalized,  # type: ignore[arg-type]
        reason=reason or "",
        agent_id=me if isinstance(me, str) else None,
    )
    return json.dumps({"success": True, **saved}, ensure_ascii=False)


def unfinished_verify_ids() -> list[str]:
    with _verify_lock:
        return [
            fid
            for fid, entry in _verify_store.items()
            if entry.get("status") == "queued"
        ]


def apply_verification_to_reports(
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """给每条漏洞报告打 verification_status；返回汇总（供 finish_scan）。

    不改 ``vulnerabilities_found`` 口径；并列给出 effective / refuted / unverified。
    """
    results = {str(r.get("finding_id")): r for r in list_verify_results()}
    refuted: list[str] = []
    unverified: list[str] = []
    upheld: list[str] = []
    for report in reports:
        rid = str(report.get("id") or "")
        if not rid:
            continue
        row = results.get(rid)
        if row is None:
            report["verification_status"] = "unverified"
            unverified.append(rid)
            continue
        status = str(row.get("status") or "")
        if status == "refuted":
            report["verification_status"] = "refuted"
            refuted.append(rid)
        elif status == "upheld":
            report["verification_status"] = "upheld"
            upheld.append(rid)
        elif status == "skipped_http":
            report["verification_status"] = "skipped_http"
        else:
            report["verification_status"] = "unverified"
            unverified.append(rid)
    effective = [
        r
        for r in reports
        if r.get("verification_status") not in {"refuted"}
    ]
    return {
        "refuted_ids": refuted,
        "unverified_ids": unverified,
        "upheld_ids": upheld,
        "vulnerabilities_effective": len(effective),
    }
