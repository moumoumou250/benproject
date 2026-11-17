"""Fact 账本工具：record_fact / list_facts（挂在 recon/enum profile）。"""

from __future__ import annotations

import json
import logging

from agents import RunContextWrapper, function_tool

from Ben.core.task_graph import add_fact, list_facts as _list_facts_impl

logger = logging.getLogger(__name__)


def _caller_id(ctx: RunContextWrapper) -> str | None:
    inner = ctx.context if isinstance(ctx.context, dict) else {}
    raw = inner.get("agent_id")
    return raw if isinstance(raw, str) else None


@function_tool(timeout=15)
async def record_fact(
    ctx: RunContextWrapper,
    kind: str,
    title: str,
    data_json: str = "{}",
    description: str = "",
    parent_fact: str | None = None,
) -> str:
    """Record a typed fact into the shared task graph (deduped by identity).

    Use for recon/enum outputs: host, service, dir, endpoint, vuln, cred, note.
    ``data_json`` is a JSON object of structured fields (host/port/url/…).
    Duplicate identity returns the existing fact_id without creating a twin.
    """
    try:
        data = json.loads(data_json) if data_json and str(data_json).strip() else {}
    except json.JSONDecodeError as exc:
        return json.dumps(
            {"success": False, "error": f"data_json must be JSON object: {exc}"},
            ensure_ascii=False,
        )
    if not isinstance(data, dict):
        return json.dumps(
            {"success": False, "error": "data_json must be a JSON object"},
            ensure_ascii=False,
        )
    result = add_fact(
        kind=kind,
        title=title,
        data=data,
        description=description,
        parent_fact=parent_fact,
        creator=_caller_id(ctx),
        derive_intent=True,
    )
    if result.get("success"):
        from Ben.agents.profiles import mark_structured_exit

        mark_structured_exit(
            ctx.context if isinstance(ctx.context, dict) else None,
            source="record_fact",
        )
    return json.dumps(result, ensure_ascii=False, default=str)


@function_tool(timeout=15)
async def list_facts(
    ctx: RunContextWrapper,
    kind: str | None = None,
    limit: int = 40,
) -> str:
    """List facts from the shared task graph (newest last)."""
    _ = ctx
    rows = _list_facts_impl(kind=kind, limit=limit)
    return json.dumps(
        {"success": True, "count": len(rows), "facts": rows},
        ensure_ascii=False,
        default=str,
    )
