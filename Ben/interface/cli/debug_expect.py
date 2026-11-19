"""检出回归：``--expect`` 匹配 + 跑完调试摘要。

与 ``--fail-on`` 极性相反——fail-on 是「发现洞就失败」（防漏报上线），
expect 是「必须检出才算过」（指定漏洞调试 / 回归）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from Ben.core.paths import RUNS_DIR_NAME

# 未命中 expect 时的退出码（与 fail_on=2、参数错=2 区分）。
EXIT_EXPECT_MISS = 3

# 工具输出里常见的闸门/拒绝信号（近似统计，供调试摘要）。
_GATE_MARKERS = (
    "不在授权白名单",
    "只读闸门",
    "read-only",
    "readonly",
    "refusing",
    "BEN_MAX_SUBAGENTS",
    "command not allowed",
    "payload",
    "不允许",
)


def normalize_expect_patterns(raw: list[str] | None) -> list[str]:
    """展开可重复的 ``--expect``；支持逗号分隔；去空白去重保序。"""
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in str(item).split(","):
            pat = part.strip()
            if not pat:
                continue
            key = pat.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(pat)
    return out


def _haystack(report: dict[str, Any]) -> str:
    parts = [
        report.get("id"),
        report.get("report_id"),
        report.get("title"),
        report.get("endpoint"),
        report.get("target"),
        report.get("cwe"),
        report.get("cve"),
        report.get("description"),
        report.get("severity"),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def finding_matches_expect(report: dict[str, Any], pattern: str) -> bool:
    """子串匹配（大小写不敏感）：命中 title/endpoint/cwe/… 任一字段即可。"""
    return pattern.lower() in _haystack(report)


def match_expect(
    reports: list[dict[str, Any]], patterns: list[str]
) -> tuple[bool, list[str], dict[str, list[str]]]:
    """每个 pattern 至少命中一条报告才算通过。

    Returns:
        (ok, missing_patterns, hits: pattern -> matching report ids)
    """
    hits: dict[str, list[str]] = {}
    missing: list[str] = []
    for pat in patterns:
        matched_ids: list[str] = []
        for report in reports:
            if finding_matches_expect(report, pat):
                rid = str(report.get("id") or report.get("report_id") or "?")
                matched_ids.append(rid)
        hits[pat] = matched_ids
        if not matched_ids:
            missing.append(pat)
    return (not missing, missing, hits)


def load_findings_json(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def count_gate_denials(invocations: list[Any]) -> int:
    """从工具输出粗略统计闸门拒绝次数。"""
    n = 0
    for inv in invocations:
        out = str(getattr(inv, "output", "") or "").lower()
        if any(m.lower() in out for m in _GATE_MARKERS):
            n += 1
    return n


def format_debug_summary(
    *,
    run_name: str,
    findings: list[dict[str, Any]],
    expect_patterns: list[str],
    invocations: list[Any] | None = None,
    expect_ok: bool | None = None,
    expect_missing: list[str] | None = None,
    expect_hits: dict[str, list[str]] | None = None,
) -> str:
    """面向终端的短摘要（指定漏洞调试用）。"""
    lines: list[str] = ["=== 调试摘要 ===", f"run: {RUNS_DIR_NAME}/{run_name}"]

    sev_counts: dict[str, int] = {}
    for report in findings:
        sev = str(report.get("severity") or "unknown").strip().lower() or "unknown"
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
    sev_part = ", ".join(f"{k}:{v}" for k, v in sorted(sev_counts.items())) or "无"
    lines.append(f"findings: {len(findings)} ({sev_part})")
    for report in findings[:12]:
        rid = report.get("id") or "?"
        sev = report.get("severity") or "?"
        title = str(report.get("title") or "(untitled)")[:80]
        ep = report.get("endpoint") or report.get("target") or ""
        suffix = f" @ {ep}" if ep else ""
        lines.append(f"  - {rid} [{sev}] {title}{suffix}")
    if len(findings) > 12:
        lines.append(f"  … 另有 {len(findings) - 12} 条")

    if expect_patterns:
        lines.append("expect:")
        hits = expect_hits or {}
        for pat in expect_patterns:
            ids = hits.get(pat) or []
            if ids:
                lines.append(f"  ✓ {pat!r} → {', '.join(ids)}")
            else:
                lines.append(f"  ✗ {pat!r} → MISS")
        if expect_ok is False and expect_missing:
            lines.append(
                f"result: EXPECT MISS ({', '.join(expect_missing)}) → exit {EXIT_EXPECT_MISS}"
            )
        elif expect_ok:
            lines.append("result: EXPECT OK")

    invs = list(invocations or [])
    if invs:
        by_name: dict[str, int] = {}
        for inv in invs:
            name = str(getattr(inv, "name", "?") or "?")
            by_name[name] = by_name.get(name, 0) + 1
        top = sorted(by_name.items(), key=lambda x: (-x[1], x[0]))[:8]
        top_s = ", ".join(f"{n}×{c}" for n, c in top)
        denials = count_gate_denials(invs)
        lines.append(f"tool calls (root layer): {len(invs)} ({top_s})")
        if denials:
            lines.append(f"gate-like refusals (approx): {denials}")

    return "\n".join(lines)
