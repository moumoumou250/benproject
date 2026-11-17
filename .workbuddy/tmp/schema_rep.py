from __future__ import annotations
from typing import Any
from agents import function_tool

@function_tool(strict_mode=False)
async def create_vulnerability_report(title: str, description: str, impact: str, target: str, technical_analysis: str, poc_description: str, poc_script_code: str, remediation_steps: str, evidence: str, assumptions: str, counterevidence: str, confidence: str, severity_change_conditions: str, fix_effort: str, cvss_breakdown: dict[str, str], endpoint: str | None=None, method: str | None=None, cve: str | None=None, cwe: str | None=None, code_locations: list[dict[str, Any]] | None=None, http_exchange_ids: list[str] | None=None, confidence_rationale: str | None=None, fix_verification: str | None=None, fix_pr_body: str | None=None) -> str:
    """File a vulnerability report — one report per fully-verified finding.

**When to file**: you have a concrete vulnerability with a working
proof-of-concept and you're 100% sure it's a real issue.

**When NOT to file**:

- General security observations without a specific vulnerability.
- Suspicions you haven't confirmed with a PoC.
- Tracking multiple vulnerabilities at once — one report per vuln.
- Re-reporting something you (or another agent) already filed.
- Known-CVE dependency / supply-chain findings that can't be
  dynamically PoC'd — file those with ``create_dependency_report``.

**Reporting and severity gate** (anti-false-positive red lines):

- A reachable endpoint, unusual response, weak configuration, or
  recon artifact is not by itself a vulnerability. File only when
  the PoC demonstrates an unauthorized security consequence.
- Score only the reasonable final impact supported by the PoC.
  Do not score speculative pivots.
- Before filing, verify that the impact narrative, PoC, and every
  non-None CVSS impact metric describe the same demonstrated
  consequence. When evidence is incomplete, lower the metric or
  continue validation; never choose a higher value "to be safe."

Automatic LLM-based **deduplication** rejects same-root-cause
duplicates. On ``duplicate_of``, revise with
``update_vulnerability_report`` — do not re-file.

**Counterevidence pass (required before filing)**: actively build
the strongest case that this finding is NOT exploitable (or less
severe), record it in ``counterevidence``, set ``confidence``
honestly, and state what would move severity in
``severity_change_conditions``. A finding you could not execute is
at best ``confidence: medium``.

**Report output rules** (this content may be rendered into reports):

- No internal/system details: never mention paths like
  ``/workspace``, internal tools, agents, sandboxes, models, or
  internal IDs (proxy request IDs, report IDs).
- Tone: formal, objective, third-person, vendor-neutral, concise.
- **Use markdown in every text field**: ``**bold**``,
  ``inline code``, and language-tagged fenced code blocks for
  code/payload/HTTP excerpts.
- Field discipline: ``poc_description`` is steps only — NO code
  (code goes in ``poc_script_code``); ``remediation_steps`` is
  prose only — NO code/diffs.

Full writing / CVSS / CWE / white-box rules: call
``load_skill("analysis/reporting_discipline")`` before your first
report. Validation errors repeat the relevant rule.

Nice to have: for code findings, if the checkout has git history, a quick
``git blame`` (quote the paths) on the vulnerable line is worth weaving into
``technical_analysis`` — who last touched it, when, and in which commit, as
part of the prose, not a separate section. Skip it if the line is
uncommitted or the command fails.


Args:
    title: Specific finding title (e.g.
        ``"SQL Injection in /api/users login parameter"``). Don't
        include the CVE number in the title.
    description: Concise, non-technical TL;DR of the vulnerability
        (1-3 sentences) — it appears first in the report. Deep
        technical detail and root-cause analysis belong in
        ``technical_analysis``, not here.
    impact: The unauthorized result demonstrated by the PoC, the
        affected data or operation, and its scope. Keep plausible
        but unverified follow-on risks separate; do not use them to
        set CVSS metrics.
    target: Affected URL / domain / repository.
    technical_analysis: The mechanism and root cause.
    poc_description: Step-by-step reproduction (steps only, no code).
    poc_script_code: Working PoC (Python preferred).
    remediation_steps: Specific, actionable fix (prose, no code).
    evidence: Concrete proof the issue is real and exploitable —
        request/response excerpts, observed behavior, tool output.
        Use fenced code blocks; no internal identifiers/paths.
    assumptions: Short note on the assumptions/prerequisites that
        make this finding impactful or exploitable (e.g. "assumes an
        authenticated low-privilege user").
    counterevidence: REQUIRED. The strongest case *against* this
        finding, after actively looking for it — the guard you might
        have missed, the deployment constraint, the precondition. If
        you genuinely found nothing, say what you checked (e.g. "no
        input validation, WAF, or authorization check found on this
        path; tested authenticated and unauthenticated"), not just
        "none". A generic trust claim ("the framework escapes this")
        is not counterevidence unless you confirmed that specific
        call in this context.
    confidence: REQUIRED. Your calibrated confidence that this is a
        real, exploitable issue: ``high`` (working PoC against the
        live target, or a complete reachable source→sink trace),
        ``medium`` (strong static evidence you could not fully
        execute), or ``low`` (plausible with a material unresolved
        gap). Do not inflate — an accurate ``medium`` is more useful
        than a ``high`` that fails triage.
    confidence_rationale: Required when ``confidence`` is not
        ``high``. Name the specific gap (e.g. "static-only trace,
        could not stand up the service to reproduce"; "reachability
        of this route from unauthenticated traffic unconfirmed").
    severity_change_conditions: REQUIRED. One concrete sentence on
        what single piece of additional evidence would raise or
        lower the severity (e.g. "confirmation this route is exposed
        to unauthenticated internet traffic would raise this to
        critical").
    fix_effort: One of ``trivial`` / ``low`` / ``medium`` / ``high``.
    cvss_breakdown: 8-metric object per the format above.
    endpoint: API path / Git path (e.g. ``/api/login``).
    method: HTTP method when relevant.
    cve: ``CVE-YYYY-NNNNN`` if certain, else omit.
    cwe: ``CWE-NNN`` (most specific child) if certain, else omit.
    code_locations: White-box findings — list of location objects.
    http_exchange_ids: Proxy request IDs that prove this finding.
        Copy these IDs from ``list_requests`` or ``view_request``.
        For a finding validated over HTTP, capture and inspect the
        supporting exchanges and include their IDs here before filing.
        Include relevant baseline/control requests as well as the exploit.
        Omit only when the finding has no captured HTTP evidence (for
        example a static-only code finding). Never invent IDs or drop
        them to bypass a verification error; retry the capture instead.
        If the result carries a ``warning`` that the IDs were not
        stored, the finding is filed without them: attach them with
        ``update_vulnerability_report`` once the proxy responds.
        Keep IDs out of ``evidence`` and all other report text.

        **How ``fix_before`` / ``fix_after`` work**: they're used as
        literal GitHub/GitLab PR suggestion blocks. When a reviewer
        accepts the suggestion, the platform replaces the **exact
        lines from ``start_line`` to ``end_line``** with
        ``fix_after``. Therefore:

        1. ``fix_before`` must be a **VERBATIM** copy of the source
           at those lines — same whitespace, indentation, line
           breaks. If it doesn't match character-for-character, the
           suggestion will corrupt the code when accepted.
        2. ``fix_after`` is the COMPLETE replacement for that
           entire block (may be more or fewer lines).
        3. ``start_line`` / ``end_line`` must precisely cover the
           lines in ``fix_before`` — no more, no less.

        **Multi-part fixes**: many fixes touch multiple
        non-contiguous parts of a file (e.g. add an import at the
        top AND change code lower down). Since each
        ``fix_before`` / ``fix_after`` pair covers ONE contiguous
        block, create **separate location entries** for each
        non-contiguous part. Use ``label`` to describe each part's
        role (``"Add escape helper import"``, ``"Sanitize input
        before SQL"``). Order primary fix first, supporting
        changes (imports, config) after.

        **Informational vs actionable**:
        - With ``fix_before`` / ``fix_after``: actionable fix
          (renders as a PR suggestion block).
        - Without them: informational context (e.g. showing the
          source of tainted data, or a sink that doesn't need
          direct editing).

        **Per-location fields**:
        - ``file`` (REQUIRED): path **relative** to repo root. No
          leading slash, no ``..``, no ``/workspace/`` prefix.
          Right: ``"src/db/queries.ts"``. Wrong:
          ``"/workspace/repo/src/db/queries.ts"``, ``"./src/x.py"``,
          ``"../../etc/passwd"``.
        - ``start_line`` (REQUIRED): 1-based; positive integer.
          Verify against the actual file — do NOT guess.
        - ``end_line`` (REQUIRED): 1-based; ``>= start_line``.
          Only equal to ``start_line`` when the block truly is one
          line.
        - ``snippet`` (optional): verbatim source at this range.
        - ``label`` (optional): short role description; especially
          important for multi-part fixes.
        - ``fix_before`` (optional): verbatim copy of the
          vulnerable code, lines ``start_line``-``end_line``.
        - ``fix_after`` (optional): complete replacement for that
          block; syntactically valid.

        **Common mistakes to avoid**:
        - Guessing line numbers instead of reading the file.
        - Paraphrasing / reformatting code in ``fix_before``.
        - Setting ``start_line == end_line`` when the vulnerable
          code spans multiple lines.
        - Bundling an import addition and a far-away code change
          into one location — split them.
        - Padding ``fix_before`` with surrounding context lines
          that aren't part of the fix.
        - Duplicating the same change across multiple locations.
    fix_verification: REQUIRED whenever any ``code_locations`` entry
        carries a ``fix_after``. A reviewer can apply that
        suggestion with one click, so an unverified fix ships
        straight into the codebase. Before writing this field, work
        the gates **in order** and never trade an earlier one for a
        later one:

        1. **Security closure** — re-trace the source → sink path
           through the *patched* code and state why it is now
           blocked. Re-run the PoC against the fix if you can.
        2. **Bypass review** — re-read the diff *without* leaning on
           the rationale that produced it. Name the sibling call
           sites, equivalent sinks, and alternate malicious input
           classes you checked, and try at least one.
        3. **Preserved behavior** — name the legitimate inputs,
           public APIs, and error semantics that must keep working,
           and confirm the patch leaves them intact. A fix that
           breaks the feature is not a fix.
        4. **Repository checks** — run the narrowest relevant
           syntax / type / lint / test check that covers the
           changed lines.

        Then write what you did: the commands you ran and their
        results, and every gate you could only reason about rather
        than execute, marked explicitly as a gap. Do not claim a
        gate passed because it looks right. If a gate fails, revise
        the patch or drop ``fix_after`` and leave the location
        informational — never compensate for a failed security
        closure with a smaller diff or extra prose.

        Also use this field to record the narrowest-complete-change
        judgement: prefer the smallest repository-native fix that
        fully enforces the invariant, using existing helpers, with
        no unrelated refactors folded in.
    fix_pr_body: Optional. When source is available and you have a
        concrete fix, a markdown PR-description body proposing the
        fix (summary + rationale). Prose/markdown only — the code
        change itself belongs in ``code_locations``. Omit for
        black-box findings."""
    return ''

@function_tool(strict_mode=False)
async def update_vulnerability_report(report_id: str, update_reason: str, title: str | None=None, description: str | None=None, impact: str | None=None, target: str | None=None, technical_analysis: str | None=None, poc_description: str | None=None, poc_script_code: str | None=None, remediation_steps: str | None=None, evidence: str | None=None, assumptions: str | None=None, counterevidence: str | None=None, confidence: str | None=None, confidence_rationale: str | None=None, severity_change_conditions: str | None=None, fix_effort: str | None=None, cvss_breakdown: dict[str, str] | None=None, endpoint: str | None=None, method: str | None=None, cve: str | None=None, cwe: str | None=None, code_locations: list[dict[str, Any]] | None=None, http_exchange_ids: list[str] | None=None, fix_verification: str | None=None, fix_pr_body: str | None=None, contextual_cvss_reasoning: str | None=None) -> str:
    """Revise a vulnerability report that is already filed, keeping its id.

Use this when you learn something a filed finding does not yet carry:

- You built the working exploit after filing the finding on static
  evidence, so the PoC and the confidence change.
- You chained the finding with another one and the real impact is
  higher, so the impact narrative and the CVSS vector change.
- Further testing narrowed or weakened the finding, so the severity
  must come down.
- Counterevidence, remediation, or a code location was wrong or
  incomplete.

This is not deduplication. You do not need a duplicate verdict to
revise your own finding, and you must not file a second report for a
finding you can revise. Call ``list_reports`` or ``get_report`` first
to find the id and read what the report already says.

Pass only the fields you want to replace. Every other field stays as
it is. Reporting rules of ``create_vulnerability_report`` apply to
every field you pass, including the markdown and tone rules.

Notes on specific fields:

- ``cvss_breakdown`` replaces the whole vector. The score and the
  severity are recalculated from it, so pass all 8 metrics. On a
  dependency finding it replaces the contextual rating and needs
  ``contextual_cvss_reasoning`` with it. Pass the reasoning alone to
  correct only the explanation of the rating already on file.
- A dependency finding never carries ``endpoint``, ``method`` or a PoC.
  File a proven exploit of the package as its own report.
- A field that only explains another field is dropped when the field
  it explains changes and you pass no replacement. Pass
  ``confidence_rationale`` with a new ``confidence``, and
  ``severity_change_conditions`` with a new ``cvss_breakdown``.
- ``code_locations`` replaces the whole list. A location carrying
  ``fix_after`` needs ``fix_verification``.

The report keeps its id, its original author, and its filing time. The
revision is recorded in the report as update history, so state the
reason plainly.

Args:
    report_id: Id of the report to revise (format ``vuln-NNNN``).
    update_reason: What you learned that the report does not yet
        carry, in one or two sentences.
    title: Replacement title.
    description: Replacement overview.
    impact: Replacement impact narrative.
    target: Replacement affected asset.
    technical_analysis: Replacement technical details.
    poc_description: Replacement PoC steps (no code).
    poc_script_code: Replacement exploit script or payload.
    remediation_steps: Replacement remediation prose (no code).
    evidence: Replacement evidence.
    assumptions: Replacement exploitability prerequisites.
    counterevidence: Replacement case against the finding.
    confidence: ``high`` / ``medium`` / ``low``.
    confidence_rationale: The gap behind a confidence below ``high``.
    severity_change_conditions: What would move the severity now.
    fix_effort: ``trivial`` / ``low`` / ``medium`` / ``high``.
    cvss_breakdown: All 8 CVSS metrics. Replaces the score and the
        severity too.
    endpoint: Replacement endpoint.
    method: Replacement HTTP method.
    cve: Replacement CVE id.
    cwe: Replacement CWE id.
    code_locations: Replacement code locations.
    http_exchange_ids: Replacement proxy request ids. Pass an empty
        list to remove all linked exchanges.
    fix_verification: Verification statement for an applyable fix.
    fix_pr_body: Replacement fix PR body.
    contextual_cvss_reasoning: Dependency findings only. What you
        observed in this codebase that justifies the contextual
        ``cvss_breakdown``."""
    return ''

@function_tool(strict_mode=False)
async def delete_vulnerability_report(report_id: str, delete_reason: str) -> str:
    """Withdraw a vulnerability report that later testing disproved.

Use this when a finding you filed turns out not to be a vulnerability
at all: the exploit only worked because of a mistake in your own test
setup (a mixed-up session, a self-inflicted state change, a misread
response), the behaviour is documented and intended, or the control
you believed was missing is in fact enforced. The report is removed
from the scan; it does not stay behind as a zero-severity entry.

Do NOT use this for a finding that is real but weaker than filed —
revise it with ``update_vulnerability_report`` so the severity, the
impact and the counterevidence come down together. Do NOT rewrite a
report into a "retracted" or "false positive" note either: delete it.

Only withdraw a report you have re-tested yourself, whoever filed it.
Call ``get_report`` first to read what it claims, then state in
``delete_reason`` exactly what disproved it, so the scan history shows
who withdrew the finding and why. A withdrawn report cannot be restored; if new
evidence later proves the issue, file it again.

Args:
    report_id: Id of the report to withdraw (format ``vuln-NNNN``).
    delete_reason: What disproved the finding, in one or two
        sentences."""
    return ''

@function_tool(strict_mode=False)
async def create_dependency_report(title: str, description: str, target: str, cve: str, package_name: str, installed_version: str, advisory_cvss: float, impact: str, remediation_steps: str, assumptions: str, package_ecosystem: str, manifest_path: str | None=None, fixed_version: str | None=None, cwe: str | None=None, technical_analysis: str | None=None, fix_effort: str='low', introduced_by: str | None=None, dependency_path: str | None=None, reachability: str='unknown', reachability_evidence: str | None=None, code_locations: list[dict[str, Any]] | None=None, fix_verification: str | None=None, contextual_cvss_breakdown: dict[str, str] | None=None, contextual_cvss_reasoning: str | None=None) -> str:
    """File a known-CVE dependency (SCA) finding — one report per CVE x package.

Use this instead of ``create_vulnerability_report`` when the finding
is a **known-CVE supply-chain issue**: a vulnerable third-party
package/version identified from a lockfile, manifest, or SBOM. Unlike
a dynamic finding, you do NOT need to trigger the vulnerability with a
live PoC — a verified advisory + the affected installed version is the
evidence.

**When to file**:

- A dependency is pinned to a version covered by a published CVE.
- You have verified the CVE ID and the installed version falls in the
  affected range (use ``web_search`` if unsure).

**When NOT to file**:

- Dynamically-proven vulnerabilities → use
  ``create_vulnerability_report`` (``finding_class`` dynamic).
- Outdated-but-not-vulnerable dependencies with no CVE.
- Re-reporting the same CVE/package already filed.

**Reachability**: do NOT silently downgrade or suppress a finding
because the vulnerable code path may be unreachable — report it, and
record what the usage analysis showed via the structured
``reachability`` + ``reachability_evidence`` fields (see the
dependency-cve-scanning skill for the analysis procedure). The level
is an evidence ladder, never an exploitability verdict:

- ``not_imported`` — the package is never imported/required by
  application code (strongest de-prioritization signal; still not
  proof of safety — dynamic loading, reflection, or framework wiring
  can evade static search).
- ``imported`` — application code imports the package, but usage of
  the advisory's affected API was not confirmed.
- ``vulnerable_symbol_used`` — the advisory's affected
  function/class/API appears in application code.
- ``reachable_call_path`` — a call-graph tool (e.g. ``govulncheck``)
  proved a path from application code to the vulnerable function.
- ``unknown`` — usage analysis was not performed or was inconclusive.

Severity comes from ``contextual_cvss_breakdown`` when you provide one
(computed exactly like a normal finding's ``cvss_breakdown``), otherwise
from ``advisory_cvss``. The reachability level alone never changes the
rating, only prioritization.

**Formatting**: use markdown in text fields (``**bold**``, ``inline
code`` for package/version identifiers, fenced code blocks for
manifest excerpts). No internal paths/tooling/agent references.

Args:
    title: e.g. ``"CVE-2024-1234 in lodash 4.17.20 (prototype pollution)"``.
    description: What the CVE is and why the pinned version is affected.
    target: Affected repository / project / manifest.
    cve: ``CVE-YYYY-NNNNN`` — required and must be verified.
    package_name: Affected package name (e.g. ``lodash``).
    installed_version: The version currently pinned/installed.
    impact: What the CVE enables; business risk in this context.
    remediation_steps: How to fix (usually upgrade to a fixed version).
    assumptions: Exploitability/reachability assumptions & confidence.
    package_ecosystem: e.g. ``npm`` / ``pypi`` / ``maven`` / ``go``.
    fixed_version: First non-vulnerable version, if known.
    cwe: ``CWE-NNN`` (most specific) if certain, else omit.
    advisory_cvss: **Required.** Published advisory base score
        (0.0-10.0) — read it off the advisory (trivy CVSS / NVD / GHSA).
        It is the published reference the finding is rated against and
        rates the finding whenever you give no contextual breakdown, so
        it must be the real published value; do not guess or omit it.
    technical_analysis: Optional deeper mechanism/root-cause detail.
    fix_effort: One of ``trivial`` / ``low`` / ``medium`` / ``high``
        (dependency upgrades are usually ``trivial``/``low``).
    introduced_by: For a **transitive** dependency, the direct
        dependency (from the project's own manifest) that pulls the
        vulnerable package in, as ``name@version`` (e.g.
        ``express@4.18.1``). Omit when the vulnerable package is
        itself a direct dependency.
    dependency_path: The resolution chain from the direct dependency
        to the vulnerable package, joined with `` > `` (e.g.
        ``express@4.18.1 > body-parser@1.20.0 > qs@6.10.2``). Omit
        for direct dependencies.
    manifest_path: **Required.** The repo-relative path of the
        lockfile/manifest where the vulnerable version was observed —
        trivy's ``Target`` (e.g. ``package-lock.json``,
        ``services/api/pom.xml``). Strip any scan-workspace or repo
        checkout directory prefix so the path is relative to the
        repository root. This binds the finding to its exact file so
        remediation can target the right repository.
    reachability: Usage-evidence level from static analysis — one of
        ``not_imported`` / ``imported`` / ``vulnerable_symbol_used`` /
        ``reachable_call_path`` / ``unknown``. Claim only what the
        evidence proves; when in doubt use ``unknown``.
    reachability_evidence: **Required.** The concrete proof for the
        claimed level, or, for ``unknown``, what you searched and why
        the result is inconclusive: repo-relative
        ``file:line`` of the import or symbol usage, the matched
        advisory symbols, or the govulncheck call-path excerpt.
        Whenever you found the vulnerable symbol in use, also give the
        **source-to-sink trace** here: start at the vulnerable package
        call site and walk backwards hop by hop to the entry point
        that carries untrusted input (HTTP route, CLI argument, queue
        message, webhook, config file), going one step deeper whenever
        a hop is a wrapper. Write it as ``entry point -> intermediate
        call -> package call`` with a ``file:line`` per hop, name what
        each hop enforces (auth, role check, validation, a flag that
        is off in production), and say who controls the input. State
        it plainly when no entry point reaches the sink — that is the
        most useful result a reader can get.
    code_locations: Where application code imports or calls the package —
        the evidence behind your ``reachability`` claim. List of dicts,
        same shape as ``create_vulnerability_report``::

            {"file": "src/api/client.ts", "start_line": 14, "end_line": 14,
             "snippet": "const x = require('pkg')", "label": "imports it"}

        Cite the repo-relative ``file`` and the exact 1-based
        ``start_line`` where ``snippet`` actually sits — verify against
        the real file, never guess. A location may also propose
        the fix itself: ``fix_before`` (verbatim current code, e.g. the
        manifest pin line) + ``fix_after`` (same lines with the fixed
        version) — that combination requires ``fix_verification``.
    fix_verification: Required whenever any ``code_locations`` entry
        carries ``fix_after``. Same bar as a normal finding: security
        closure, bypass review, preserved behavior, and how each was
        checked.
    contextual_cvss_breakdown: **Required.** Full CVSS v3.1 rating of this
        CVE **in this codebase** — the same 8-metric object as
        ``create_vulnerability_report``'s ``cvss_breakdown``:
        ``attack_vector`` (N/A/L/P), ``attack_complexity`` (L/H),
        ``privileges_required`` (N/L/H), ``user_interaction`` (N/R),
        ``scope`` (U/C), ``confidentiality`` / ``integrity`` /
        ``availability`` (N/L/H). All 8 metrics are required when the
        field is set, and the contextual score/vector are computed
        from them — you never supply a score. Start from the
        advisory's published metrics and change only what the
        **source-to-sink trace** you recorded in
        ``reachability_evidence`` proves is different here: derive
        ``attack_vector`` / ``privileges_required`` /
        ``user_interaction`` from what the entry point actually
        requires, ``attack_complexity`` from the preconditions the
        hops enforce, and the impact metrics from the data and
        privileges reachable at the sink. When provided, this rating
        determines the finding's severity; ``advisory_cvss`` stays as
        the published reference. Send it on every report: when the
        trace does not change the published rating, or when you could
        not complete the trace, repeat the advisory's own metrics and
        adjust only what the usage level itself proves (a package the
        code never imports is normally ``N`` on all three impact
        metrics), then say so in the reasoning.
    contextual_cvss_reasoning: **Required.** Two to four detailed
        sentences that a reviewer can verify without opening the repo:
        how the application uses the package, which call sites or
        configuration you inspected (repo-relative ``file:line``),
        which input reaches the vulnerable code and whether an
        attacker controls it, and what the adjustment therefore
        changes. State the source-to-sink chain explicitly, hop by
        hop, as ``entry point -> intermediate call -> package call``
        with a ``file:line`` for each hop. Cite concrete evidence,
        never a generic statement such as "low risk". The user reads
        this text next to the adjusted score, so an adjustment
        without it is discarded."""
    return ''

@function_tool(strict_mode=False)
async def list_reports(severity: str | None=None, finding_class: str | None=None, target: str | None=None, search: str | None=None, include_details: bool=False) -> str:
    """List vulnerability reports filed so far in this scan — metadata-first.

**For the orchestrator / root agent.** This is an orchestration tool
for tracking scan-wide coverage and assembling the final report — leaf
/ specialist agents do their own testing and file findings; they should
NOT call this. If you are a subagent, ignore it and focus on your task.

Reports are shared across **every** agent in the scan, so this returns
findings filed by any agent (root or child), not just your own. As the
root agent, use it to track progress, avoid dispatching work on
already-covered ground, reason about attack-chaining across confirmed
findings, and build the ``finish_scan`` executive summary.

By default each entry is compact: ``id``, ``title``, ``severity``,
``cvss``, ``confidence``, ``finding_class``, ``cve`` / ``cwe``,
``target`` / ``endpoint``, ``fix_effort``, ``agent_name`` (who filed it), ``timestamp``,
plus a 280-char ``description_preview``. Entries you filed yourself are
flagged ``by_you: true``. The response also carries
``total_count`` and ``severity_counts`` (counts per severity across all
reports, ignoring filters). Set ``include_details=True`` for full report
bodies (PoC, evidence, remediation, code_locations) — token-expensive;
prefer ``get_report`` to drill into a single finding.

Filters compose (all must match): ``severity`` and ``finding_class``
match exactly, ``target`` is a substring match against target/endpoint,
and ``search`` is a substring match against title/description. Results
are ordered by severity (critical -> info), then report id.

This is read-only — it never files or dedupes anything.

Args:
    severity: Filter to one of ``critical`` / ``high`` / ``medium`` /
        ``low`` / ``info`` / ``none``.
    finding_class: Filter to ``dynamic`` (PoC-backed) or
        ``dependency_cve`` (known-CVE supply-chain).
    target: Substring match against a report's target / endpoint.
    search: Substring match against title and description.
    include_details: When False (default) entries are compact; when
        True full report bodies are returned."""
    return ''

@function_tool(strict_mode=False)
async def get_report(report_id: str) -> str:
    """Fetch one vulnerability report by its id (e.g. ``vuln-0001``).

Returns the full report body — description, impact, technical analysis,
PoC, evidence, remediation, CVSS breakdown, and any ``code_locations``.
Use ``list_reports`` first to find ids; this is the cheap way to read a
single finding in full without pulling every body.

Read-only.

Args:
    report_id: Report id from ``list_reports`` or a
        ``create_vulnerability_report`` / ``create_dependency_report``
        response (format ``vuln-NNNN``)."""
    return ''

