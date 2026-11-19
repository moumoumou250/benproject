"""``finish_scan`` — root-agent termination + executive report persistence."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents import RunContextWrapper, function_tool

from Ben.core.agents import coordinator_from_context


logger = logging.getLogger(__name__)


def _do_finish(
    *,
    parent_id: str | None,
    executive_summary: str,
    methodology: str,
    technical_analysis: str,
    recommendations: str,
    agent_graph: dict[str, Any],
) -> dict[str, Any]:
    if parent_id is not None:
        return {
            "success": False,
            "error": (
                "This tool can only be used by the root/main agent. "
                "If you are a subagent, use agent_finish instead"
            ),
        }

    errors: list[str] = []
    if not executive_summary.strip():
        errors.append("Executive summary cannot be empty")
    if not methodology.strip():
        errors.append("Methodology cannot be empty")
    if not technical_analysis.strip():
        errors.append("Technical analysis cannot be empty")
    if not recommendations.strip():
        errors.append("Recommendations cannot be empty")
    if errors:
        return {"success": False, "error": "Validation failed", "errors": errors}

    verify_summary: dict[str, Any] = {
        "refuted_ids": [],
        "unverified_ids": [],
        "upheld_ids": [],
        "vulnerabilities_effective": 0,
    }
    try:
        from Ben.report.state import get_global_report_state

        report_state = get_global_report_state()
        if report_state is None:
            logger.warning("No global report state; scan results not persisted")
            return {
                "success": True,
                "scan_completed": True,
                "message": "Scan completed (not persisted)",
                "warning": "Results could not be persisted - report state unavailable",
            }
        # H4：消费 verify 队列，标注 verification_status（不改漏洞总数口径）。
        from Ben.tools.verify.tool import apply_verification_to_reports

        verify_summary = apply_verification_to_reports(
            report_state.vulnerability_reports
        )
        tech = technical_analysis.strip()
        refuted_ids = list(verify_summary.get("refuted_ids") or [])
        unverified_ids = list(verify_summary.get("unverified_ids") or [])
        if refuted_ids or unverified_ids:
            appendix_lines = ["", "## Verification appendix"]
            if unverified_ids:
                appendix_lines.append(
                    "Unverified findings (queued or never sent): "
                    + ", ".join(f"`{i}`" for i in unverified_ids)
                )
            if refuted_ids:
                appendix_lines.append(
                    "Refuted / not adopted (excluded from findings body): "
                    + ", ".join(f"`{i}`" for i in refuted_ids)
                )
            tech = tech + "\n".join(appendix_lines)
        # 刀 B：正文里 unverified 标题加标记；refuted 标题注明已证伪。
        for report in report_state.vulnerability_reports:
            title = str(report.get("title") or "")
            status = report.get("verification_status")
            if status == "unverified" and title and "[未验证]" not in title:
                report["title"] = f"[未验证] {title}"
            elif status == "refuted" and title and "[已证伪]" not in title:
                report["title"] = f"[已证伪] {title}"
        vuln_count = len(report_state.vulnerability_reports)
        coverage_summary = _coverage_summary(agent_graph)
        # strict：先拒收，不落盘终稿，逼 root 清 pending
        if coverage_summary.get("success") is False:
            blocked: dict[str, Any] = {
                "success": False,
                "scan_completed": False,
                "message": str(
                    coverage_summary.get("error") or "Coverage gate blocked finish_scan"
                ),
                "vulnerabilities_found": vuln_count,
            }
            blocked.update(verify_summary)
            blocked.update(coverage_summary)
            return blocked
        report_state.update_scan_final_fields(
            executive_summary=executive_summary.strip(),
            methodology=methodology.strip(),
            technical_analysis=tech,
            recommendations=recommendations.strip(),
        )
    except (ImportError, AttributeError) as e:
        logger.exception("finish_scan persistence failed")
        return {"success": False, "error": f"Failed to complete scan: {e!s}"}
    else:
        logger.info(
            "finish_scan: completed scan with %d vulnerability report(s)",
            vuln_count,
        )
        result: dict[str, Any] = {
            "success": True,
            "scan_completed": True,
            "message": "Scan completed successfully",
            "vulnerabilities_found": vuln_count,
        }
        result.update(verify_summary)
        result.update(coverage_summary)
        return result


def _coverage_gate_mode() -> str:
    """``warn``（默认）或 ``strict``。其它值按 warn 处理。"""
    import os

    raw = (os.environ.get("BEN_COVERAGE_GATE") or "warn").strip().lower()
    return "strict" if raw == "strict" else "warn"


def _coverage_summary(agent_graph: dict[str, Any]) -> dict[str, Any]:
    """Coverage counts, unresolved surfaces, and gaps the runtime can see.

    Distinguishes ``pending`` (never tried) from ``needs_follow_up`` (tried,
    no conclusion). ``BEN_COVERAGE_GATE=strict`` refuses finish while either
    remains open.
    """
    from Ben.report.coverage import agents_from_graph, skill_coverage_gaps
    from Ben.tools.coverage.tools import get_coverage_entries, outcome_counts

    entries = get_coverage_entries()
    if not entries:
        return {
            "coverage_recorded": 0,
            "coverage_warning": (
                "No coverage was recorded for this scan. The report cannot show which "
                "surfaces were reviewed and cleared — only what was found. Use "
                "record_coverage during testing so future scans can report negative space."
            ),
        }

    counts = outcome_counts()
    summary: dict[str, Any] = {
        "coverage_recorded": len(entries),
        "coverage_outcomes": counts,
    }
    pending = [e for e in entries if e.get("outcome") == "pending"]
    follow_up = [e for e in entries if e.get("outcome") == "needs_follow_up"]
    unresolved = [*pending, *follow_up]
    if unresolved:
        parts: list[str] = []
        if pending:
            parts.append(f"{len(pending)} pending (never assessed)")
        if follow_up:
            parts.append(f"{len(follow_up)} needs_follow_up")
        summary["coverage_warning"] = (
            f"{len(unresolved)} surface(s) still open ({'; '.join(parts)}). "
            "Represent them in the report as Not covered / further review — "
            "do not omit them."
        )
        summary["unresolved_surfaces"] = [
            {
                "surface": e.get("surface", ""),
                "risk_area": e.get("risk_area", ""),
                "outcome": e.get("outcome", ""),
                "entry_id": e.get("entry_id", ""),
                "evidence": e.get("evidence", ""),
            }
            for e in unresolved
        ]
        summary["pending_surfaces"] = [
            {"surface": e.get("surface", ""), "risk_area": e.get("risk_area", ""),
             "entry_id": e.get("entry_id", "")}
            for e in pending
        ]

    gaps = skill_coverage_gaps(entries, agents_from_graph(agent_graph))
    if gaps:
        summary["coverage_gaps"] = [gap["detail"] for gap in gaps]
        summary["coverage_gap_warning"] = (
            f"{len(gaps)} risk class(es) assigned to agents have no coverage entry and "
            "will be published as unexamined. Record them (or a needs_follow_up row) "
            "before the report goes out."
        )

    if unresolved and _coverage_gate_mode() == "strict":
        summary["success"] = False
        summary["scan_completed"] = False
        summary["error"] = (
            "BEN_COVERAGE_GATE=strict: refuse finish_scan while pending / "
            "needs_follow_up rows remain. Dispatch agents or update_coverage "
            "(not_applicable with evidence) to clear them."
        )
        summary["coverage_gate"] = "strict"
    else:
        summary["coverage_gate"] = _coverage_gate_mode()
    return summary


@function_tool(timeout=60)
async def finish_scan(
    ctx: RunContextWrapper,
    executive_summary: str,
    methodology: str,
    technical_analysis: str,
    recommendations: str,
) -> str:
    """Finalize the scan — persist the customer-facing report.

    **Root-agent only.** Subagents must call ``agent_finish`` from the
    multi-agent graph tools instead. Calling this finalizes everything:

    1. Verifies you are the root agent.
    2. Writes the four narrative sections to the scan record.
    3. Marks the scan completed and stops execution.

    **This is a terminal action, not a status probe.** Whatever you pass
    is persisted VERBATIM as the final, customer-facing report and then
    execution stops. There is no draft mode and no second chance: never
    submit placeholder, provisional, or "checking if done" text in any
    field, and never call ``finish_scan`` to poll whether subagents are
    done (use ``view_agent_graph`` / ``wait_for_agents`` for that).
    Call it exactly ONCE, only when every field holds genuine, finished
    assessment prose.

    **Pre-flight checklist (mandatory — do not skip):**

    1. **Call ``view_agent_graph`` first.** Inspect every entry in the
       summary. If ANY agent is in ``running`` / ``waiting`` state,
       you MUST NOT call ``finish_scan`` yet —
       wrap them up first via ``send_message_to_agent`` (ask them to
       finish), ``wait_for_agents`` (block until their report
       arrives), or ``stop_agent`` (graceful cancel). Only ``completed``
       / ``crashed`` / ``stopped`` agents are safe to leave behind.
       Calling ``finish_scan`` while children are alive orphans their
       work and produces an incomplete report.
    2. It's a good idea to call ``list_reports`` before finishing to
       review every finding filed in this scan (use ``get_report`` for
       full detail on any of them) so your ``executive_summary`` /
       ``technical_analysis`` are grounded in what was actually reported
       — don't invent or omit findings. All vulnerabilities you found are
       filed via ``create_vulnerability_report`` — or, for known-CVE
       dependency findings, ``create_dependency_report`` (un-reported
       findings are not tracked and not credited). A dependency CVE
       already filed via ``create_dependency_report`` counts as reported;
       it does NOT need re-filing here and does NOT block finishing.
    3. Don't double-report — one report per distinct vulnerability.
    4. **Attack-chaining gate.** Do NOT finish until you have genuinely
       considered chaining the confirmed findings into higher-impact,
       end-to-end attack paths and tested every plausibly-related
       combination. You may rule out combinations you can confidently
       call unrelated — note why instead of padding chains. Any
       validated chain must already be filed via
       ``create_vulnerability_report`` — a demonstrated end-to-end chain
       is a PoC-backed vulnerability, so it uses that tool even when one
       link is a dependency CVE (the standalone CVE stays in its own
       ``create_dependency_report``) — and surfaced prominently in
       ``executive_summary`` / ``technical_analysis``. Finding no real
       chain after a serious attempt is acceptable; skipping the
       chaining reasoning, or ignoring a plausibly-related combination,
       is not.
    5. **Coverage reconciliation.** Call ``list_coverage`` and clear
       both ``pending`` (inventory seeded, never assessed — system-
       assigned work) and ``needs_follow_up`` (tried, unresolved).
       Close via ``update_coverage`` on the existing ``entry_id``, or
       waive obvious non-targets with ``bulk_close_coverage`` /
       ``not_applicable`` plus evidence — never a silent omission.
       The response reports coverage counts and any unresolved rows.

    **Calling this multiple times overwrites the previous report.**
    Make the single call comprehensive.

    **Report output rules** (this content may be rendered into generated
    reports):

    - Never mention internal infrastructure: no local/absolute paths
      (``/workspace/...``), no agent names, no sandbox/orchestrator/
      tooling references, no system prompts, no model-internal errors.
      Never leak internal identifiers (proxy request IDs, internal
      vulnerability report IDs, or any system-generated IDs) into any
      field.
    - Tone: formal, third-person, objective, concise. This is a
      consultant deliverable, not an engineering log.
    - Each section has a specific role:

        - ``executive_summary`` — for non-technical leadership. Risk
          posture, business impact (data exposure / compliance /
          reputation), notable criticals, overarching remediation
          theme.
        - ``methodology`` — frameworks followed (OWASP WSTG, PTES,
          OSSTMM, NIST), engagement type (black/gray/white box), scope
          and constraints, categories of testing performed. **No**
          internal execution detail.
        - ``technical_analysis`` — consolidated findings overview with
          severity model and systemic root causes. Reference individual
          vuln reports for repro steps; don't duplicate raw evidence.
        - ``recommendations`` — prioritized actions grouped by urgency
          (Immediate / Short-term / Medium-term), each with concrete
          remediation steps. End with retest/validation guidance.

    - **Formatting — use markdown in every field.** These fields may be
      rendered into generated reports, so structure them clearly: use
      ``**bold**`` for labels/emphasis, ``inline code`` for
      identifiers/paths/parameters, bullet or numbered lists for
      enumerations, and fenced code blocks (```` ```language ````) for
      any code/payload excerpts. Never emit one flat wall of prose or
      leave code unformatted.
    - **Each field is a section body, not a document.** The report adds
      the section title ("Executive Summary", "Methodology", ...) itself,
      so do NOT start a field with a heading such as ``# Executive
      Summary`` — it would print twice. Sub-headings (``##``) inside a
      field are fine.
    - If **zero** vulnerabilities were found, say so plainly and
      characterize the posture positively; ``technical_analysis`` should
      summarize the areas tested and confirm no issues, and
      ``recommendations`` should focus on general hardening.

    Example (abbreviated — mirror this structure, not the wording)::

        executive_summary:
            An external assessment of the **Acme Customer Portal**
            identified multiple weaknesses that could lead to
            unauthorized access to customer data.

            **Overall risk posture:** Elevated.

            **Key findings**
            - Confirmed SSRF in a URL-preview feature reaching internal
              network ranges.
            - Broken tenant isolation enabling cross-tenant data access.

            **Business impact**
            - Potential exposure of customer records across tenants.

        methodology:
            Conducted per the **OWASP WSTG**.

            **Engagement type:** Gray-box external test.
            **Scope:** `https://app.acme.example`, `.../api/v1/`.

            **Activities:** recon, authn/session review, authorization
            and tenant-isolation testing, input/SSRF testing.

        technical_analysis:
            **Severity model** reflects exploitability x impact.

            1. **SSRF in URL preview** (Critical) — insufficient
               destination validation; reaches link-local addresses.
            2. **Broken tenant isolation** (High) — object identifiers
               accepted without ownership checks.

            **Systemic themes:** authorization enforced inconsistently;
            no deny-by-default egress policy.

        recommendations:
            **Immediate**
            1. Remediate SSRF: enforce a destination allowlist,
               deny-by-default, re-validate on every redirect hop.

            **Short-term**
            2. Centralize authorization with deny-by-default middleware.

            **Retest & validation:** re-test immediate items to confirm
            SSRF and tenant-isolation controls hold.

    Args:
        executive_summary: Business-level summary for leadership.
        methodology: Frameworks, scope, and approach.
        technical_analysis: Consolidated findings + systemic themes.
        recommendations: Prioritized, actionable remediation.
    """
    inner = ctx.context if isinstance(ctx.context, dict) else {}
    coordinator = coordinator_from_context(inner)
    me = inner.get("agent_id")
    parent_id = inner.get("parent_id")
    if coordinator is not None and parent_id is None and me is not None:
        active_agents = await coordinator.active_agents_except(me)
        if active_agents and coordinator.reserve_stopped:
            active_agents = []
    else:
        active_agents = []

    if active_agents:
        return json.dumps(
            {
                "success": False,
                "scan_completed": False,
                "error": (
                    "Cannot finish scan while child agents are still active. "
                    "Wait for completion, send them finish instructions, or stop them first"
                ),
                "active_agents": active_agents,
            },
            ensure_ascii=False,
            default=str,
        )

    result = await asyncio.to_thread(
        _do_finish,
        parent_id=parent_id,
        executive_summary=executive_summary,
        methodology=methodology,
        technical_analysis=technical_analysis,
        recommendations=recommendations,
        agent_graph=await coordinator.snapshot() if coordinator is not None else {},
    )
    if (
        result.get("success")
        and result.get("scan_completed")
        and coordinator is not None
        and isinstance(me, str)
    ):
        await coordinator.set_status(me, "completed")
    return json.dumps(result, ensure_ascii=False, default=str)
