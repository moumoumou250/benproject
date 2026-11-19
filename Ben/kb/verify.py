"""``ben skill verify``：正样本上 skill 能否复现。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from Ben.core.stopped import StoppedCode, coerce_stopped_code
from Ben.kb.actions import recommend
from Ben.kb.contract import ExitCode, Result, SubReason
from Ben.kb.criteria import extract
from Ben.kb.env import env_flag
from Ben.kb.evidence import (
    Evidence,
    EvidenceKind,
    blocked_features,
    collect_from_findings,
    collect_from_http_dir,
    is_confirmable_kind,
    last_http_parse_errors,
    materialize_finding_exchanges,
    paths_seen,
    paths_seen_from_agents_db,
    surface_hit,
)
from Ben.kb.evidence_ingest import merge_workspace_evidence
from Ben.kb.oracle import oracle_to_dict, run_oracle
from Ben.kb.profile import ProfileError, assert_profile_allowed, load_profile
from Ben.kb.vocab import canonical, literal_lookup, normalize
from Ben.kb.verdict import (
    VerdictResult,
    build_verdicts,
    confirm,
    validate_verdict_payload,
    verdicts_to_json,
)

_ARTIFACTS = Path(__file__).resolve().parents[2] / "_artifacts" / "kb_evidence"


@dataclass(frozen=True)
class ScanOutcome:
    """扫描结果契约；生产路径不自动填 mechanical。"""

    stopped_code: StoppedCode = StoppedCode.UNKNOWN
    stopped_reason: str = ""
    run_name: str | None = None
    exit_code: int | None = None
    evidence: tuple[Evidence, ...] = ()
    # 仅测试/显式注入；从不因 surface_hit 自动生成
    mechanical: dict[int, tuple[VerdictResult, list[str]]] | None = None
    kb_evidence: dict[str, Any] = field(default_factory=dict)


def _evidence_dir(term: str, profile: str, out_dir: Path | None) -> Path:
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _ARTIFACTS / f"{stamp}_{normalize(term)}_{profile}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_skill_body(name: str) -> str:
    from Ben.skills import list_skills

    for skill in list_skills().values():
        if skill.category == "vulnerabilities" and skill.name == name:
            return skill.body
    return ""


def _coerce_scan_outcome(raw: Any) -> ScanOutcome:
    if isinstance(raw, ScanOutcome):
        return raw
    if not isinstance(raw, dict):
        return ScanOutcome()
    mech = raw.get("mechanical")
    mechanical: dict[int, tuple[VerdictResult, list[str]]] | None = None
    if isinstance(mech, dict) and mech:
        parsed: dict[int, tuple[VerdictResult, list[str]]] = {}
        for k, v in mech.items():
            if not isinstance(v, (tuple, list)) or len(v) != 2:
                continue
            result, refs = v[0], v[1]
            if result not in {"PASS", "FAIL", "UNOBSERVED", "SKIPPED"}:
                continue
            if not isinstance(refs, list):
                continue
            parsed[int(k)] = (result, [str(r) for r in refs])
        mechanical = parsed or None

    # 已删除的假契约键一律丢弃，不进入 ScanOutcome
    evidence_items = tuple(

        e for e in (raw.get("evidence") or []) if isinstance(e, Evidence)
    )
    kb = raw.get("kb_evidence") if isinstance(raw.get("kb_evidence"), dict) else {}
    return ScanOutcome(
        stopped_code=coerce_stopped_code(raw.get("stopped_code")),
        stopped_reason=str(raw.get("stopped_reason") or ""),
        run_name=raw.get("run_name"),
        exit_code=raw.get("exit_code"),
        evidence=evidence_items,
        mechanical=mechanical,
        kb_evidence=dict(kb),
    )


def verify_term(
    term: str,
    *,
    profile_name: str,
    mode: str = "scan",
    max_budget: float | None = None,
    max_turns: int | None = None,
    minutes: int | None = None,
    out_dir: Path | None = None,
    oracle_only: bool = False,
    run_scan: Callable[..., Any] | None = None,
) -> Result:
    """正样本验证主路径。``run_scan`` 可注入假 runner（测试用）。"""
    raw = (term or "").strip()
    if not raw or not profile_name:
        return Result(ExitCode.USAGE, message="需要 <term> 与 --profile")

    resolved = literal_lookup(raw) or canonical(raw)
    if resolved is None:
        return Result(
            ExitCode.USAGE,
            message=f"词表外：{raw!r}",
            action=recommend(verify_code=ExitCode.USAGE, check_code=ExitCode.NEGATIVE).to_dict(),
        )

    try:
        profile = load_profile(profile_name)
        assert_profile_allowed(profile, term=resolved)
    except ProfileError as exc:
        return Result(ExitCode.USAGE, message=str(exc))
    except ValueError as exc:
        return Result(ExitCode.USAGE, message=str(exc))

    body = _load_skill_body(resolved)
    criteria = extract(body, "Validation")
    if not criteria:
        return Result(
            ExitCode.INCONCLUSIVE,
            subreason=SubReason.NO_VALIDATION,
            message="NO_VALIDATION",
            action=recommend(
                verify_code=ExitCode.INCONCLUSIVE,
                check_code=ExitCode.OK,
                subreason=SubReason.NO_VALIDATION,
            ).to_dict(),
        )

    evidence_dir = _evidence_dir(resolved, profile.name, out_dir)
    oracle = run_oracle(profile, evidence_dir=evidence_dir)
    if not oracle.ok:
        return Result(
            oracle.code,
            subreason=oracle.subreason,
            message=oracle.message,
            extra={
                "oracle": oracle_to_dict(oracle),
                "evidence_dir": str(evidence_dir),
                "term": raw,
                "resolved": f"vulnerabilities/{resolved}",
                "profile": profile.name,
            },
            action=None,
        )

    if oracle_only or mode == "probe":
        payload = {
            "schema": "ben.kb.verdict/1",
            "term": raw,
            "resolved": f"vulnerabilities/{resolved}",
            "profile": profile.name,
            "expects_vuln": profile.expects_vuln,
            "target": profile.targets[0],
            "code": int(ExitCode.OK),
            "verdict": "ORACLE_OK",
            "subreason": None,
            "oracle": oracle_to_dict(oracle),
            "surface_hit": None,
            "evidence_dir": str(evidence_dir),
            "mode": "probe",
            "criteria": [],
            "action": None,
        }
        (evidence_dir / "verdict.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return Result(
            ExitCode.OK,
            message="ORACLE_OK（probe/oracle-only，未启扫描）",
            extra=payload,
        )

    # —— scan 模式 ——
    if not env_flag("BEN_KB_EVIDENCE"):
        os.environ["BEN_KB_EVIDENCE"] = "1"
    # P4：AuditWriter 已接线，verify 默认开录像（BEN_AUDIT=0 可关）
    if not env_flag("BEN_AUDIT") and os.environ.get("BEN_AUDIT") is None:
        os.environ["BEN_AUDIT"] = "1"

    if run_scan is not None:
        outcome = _coerce_scan_outcome(
            run_scan(profile=profile, term=resolved, evidence_dir=evidence_dir)
        )
    else:
        outcome = _default_run_scan(
            profile,
            term=resolved,
            max_budget=max_budget,
            max_turns=max_turns,
            minutes=minutes,
        )

    run_name = outcome.run_name
    stopped_code = outcome.stopped_code
    kb_stats = dict(outcome.kb_evidence)
    (evidence_dir / "scan.json").write_text(
        json.dumps(
            {
                "run_name": run_name,
                "stopped_code": str(stopped_code),
                "stopped_reason": outcome.stopped_reason,
                "kb_evidence": kb_stats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if stopped_code == StoppedCode.BUDGET:
        return Result(
            ExitCode.INCONCLUSIVE,
            subreason=SubReason.BUDGET,
            message="BUDGET",
            extra={"evidence_dir": str(evidence_dir), "scan": {"run_name": run_name}},
        )
    if stopped_code in {StoppedCode.ERROR, StoppedCode.INTERRUPTED} and not outcome.evidence:
        return Result(
            ExitCode.INCONCLUSIVE,
            subreason=SubReason.CONNECTION_LOST,
            message="CONNECTION_LOST",
            extra={"evidence_dir": str(evidence_dir), "scan": {"run_name": run_name}},
        )

    http_dir = evidence_dir / "http"
    findings_http = evidence_dir / "findings_http"
    evidence: list[Evidence] = collect_from_http_dir(http_dir, kind=EvidenceKind.E2)
    run_dir: Path | None = None
    if run_name:
        from Ben.core.paths import resolve_run_dir

        run_dir = resolve_run_dir(str(run_name))
        if not kb_stats:
            kb_file = run_dir / "kb_evidence.json"
            if kb_file.is_file():
                try:
                    kb_stats = json.loads(kb_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    kb_stats = {}
        evidence = merge_workspace_evidence(run_dir, http_dir)
        evidence.extend(materialize_finding_exchanges(run_dir, findings_http))
        evidence.extend(collect_from_findings(run_dir))

    evidence.extend(outcome.evidence)
    parse_errors = last_http_parse_errors()

    oracle_path = profile.oracle.path if profile.oracle else ""
    hit = surface_hit(evidence, oracle_path)

    blocked = False
    if http_dir.is_dir():
        for resp in http_dir.rglob("*.resp"):
            if blocked_features(resp.read_text(encoding="utf-8", errors="replace")):
                blocked = True
                break

    e1e2 = [e for e in evidence if is_confirmable_kind(e.kind)]
    copy_errors = list(kb_stats.get("errors") or []) if isinstance(kb_stats, dict) else []
    if copy_errors and not e1e2:
        return Result(
            ExitCode.INCONCLUSIVE,
            subreason=SubReason.UNREADABLE,
            message=f"KB 证据拷贝失败：{'; '.join(str(x) for x in copy_errors[:3])}",
            extra={
                "evidence_dir": str(evidence_dir),
                "scan": {"run_name": run_name, "kb_evidence": kb_stats},
            },
        )

    # 仅显式 mechanical；禁止 surface_hit 自动 PASS
    mechanical: dict[int, tuple[VerdictResult, list[str]]] = dict(outcome.mechanical or {})

    verdicts = build_verdicts(
        criteria,
        expected_indices=profile.expected_criteria,
        mechanical=mechanical,
    )
    confirmed = confirm(
        criteria,
        verdicts,
        evidence,
        expects_vuln=profile.expects_vuln,
        surface_hit=hit,
        blocked=blocked,
    )

    from Ben.kb.criteria import has_bypass_section

    action = recommend(
        check_code=ExitCode.OK if literal_lookup(raw) else ExitCode.NEGATIVE,
        verify_code=confirmed.code,
        subreason=confirmed.subreason,
        has_bypass=has_bypass_section(body),
        expects_vuln=profile.expects_vuln,
        kb_file=f"Ben/skills/vulnerabilities/{resolved.replace('-', '_')}.md",
    )

    seen = paths_seen(evidence)
    hint_db: list[str] = []
    if run_dir is not None:
        hint_db = paths_seen_from_agents_db(run_dir)

    payload: dict[str, Any] = {
        "schema": "ben.kb.verdict/1",
        "term": raw,
        "resolved": f"vulnerabilities/{resolved}",
        "profile": profile.name,
        "expects_vuln": profile.expects_vuln,
        "target": profile.targets[0],
        "code": int(confirmed.code),
        "verdict": _verdict_label(confirmed.code, confirmed.subreason),
        "subreason": str(confirmed.subreason) if confirmed.subreason else None,
        "oracle": oracle_to_dict(oracle),
        "surface_hit": hit,
        "paths_seen": seen,
        "paths_hint_db": hint_db,
        "evidence_dir": str(evidence_dir),
        "scan": {
            "run_name": run_name,
            "stopped_code": str(stopped_code),
            "kb_evidence": kb_stats,
        },
        "criteria_total": len(criteria),
        "criteria_expected": list(profile.expected_criteria),
        "criteria": verdicts_to_json(verdicts),
        "action": action.to_dict() if action else None,
    }
    if parse_errors:
        payload["evidence_parse_errors"] = parse_errors
    # transcript 指针（E0）
    if run_dir is not None:
        audit_dir = Path(run_dir) / "audit"
        if audit_dir.is_dir():
            from Ben.core.audit import load_events

            events = load_events(audit_dir)
            index_path = audit_dir / "index.json"
            index_meta: dict[str, Any] = {}
            if index_path.is_file():
                try:
                    index_meta = json.loads(index_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    index_meta = {}
            payload["transcript"] = {
                "dir": str(audit_dir),
                "events": index_meta.get("events", len(events)),
                "agents": index_meta.get("agents", 0),
                "bytes": index_meta.get("bytes", 0),
                "truncated_items": index_meta.get("truncated_items", 0),
                "capped": bool(index_meta.get("capped")),
                "secret_hits": index_meta.get("secret_hits", 0),
                "view": f"ben transcript {run_name}",
            }

    if confirmed.subreason == SubReason.MISS_SURFACE:
        closest = ""
        if hint_db:
            # 简单差集句：列出已请求、标出 oracle
            closest = f"；agents.db 提示：{', '.join(hint_db[:8])}"
        payload["hint"] = (
            f"本 run e1/e2 路径：{', '.join(seen) or '（无）'}{closest}；"
            f"oracle 要 {oracle_path}"
        )
    elif confirmed.subreason == SubReason.BLOCKED:
        payload["hint"] = "响应含拦截特征（Cloudflare/WAF 等）；检查 Bypass Techniques 是否覆盖"
    elif confirmed.subreason == SubReason.FALSE_NEGATIVE:
        payload["hint"] = "必需判据 FAIL；请用 ben transcript 回看放弃前的 assistant 文本与工具回包"
    elif confirmed.subreason == SubReason.TECHNIQUE_MISS:
        payload["hint"] = (
            f"已打到面但手法不符；oracle 路径 {oracle_path}；"
            f"本 run e1/e2：{', '.join(seen) or '（无）'}"
        )

    schema_errs = validate_verdict_payload(payload)
    if schema_errs:
        payload["schema_errors"] = schema_errs

    (evidence_dir / "verdict.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (evidence_dir / "criteria.json").write_text(
        json.dumps(verdicts_to_json(verdicts), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return Result(
        confirmed.code,
        subreason=confirmed.subreason,
        message=confirmed.message or payload["verdict"],
        rows=verdicts_to_json(verdicts),
        action=action.to_dict() if action else None,
        extra=payload,
    )


def _verdict_label(code: ExitCode, sub: SubReason | None) -> str:
    if code == ExitCode.OK:
        return "CONFIRMED"
    if code == ExitCode.NEGATIVE:
        return str(sub or "FALSE_NEGATIVE")
    if code == ExitCode.USAGE:
        return "USAGE"
    return str(sub or "INCONCLUSIVE")


def _default_run_scan(
    profile: Any,
    *,
    term: str,
    max_budget: float | None,
    max_turns: int | None,
    minutes: int | None,
) -> ScanOutcome:
    """通过 ``ben vuln`` 聚焦扫描；返回 ScanOutcome（无机械自动 PASS）。"""
    from Ben.interface.cli.vuln import run_vuln
    from Ben.interface.main import _execute_task

    budget = max_budget if max_budget is not None else profile.max_budget_usd
    turns = max_turns if max_turns is not None else profile.max_turns
    hint = (getattr(profile, "verify_hint", None) or profile.description or term).strip()
    oracle_path = profile.oracle.path if profile.oracle else "/"
    argv = [
        term,
        "-t",
        profile.targets[0],
        "--max-budget",
        str(budget),
        "--max-turns",
        str(turns),
        "--instruction",
        (
            f"正样本验证：目标已知存在 {term}（{hint}）。"
            f"优先探测 {oracle_path}；"
            "每次判定性请求把原始 HTTP 写到 /workspace/.evidence/："
            "扁平文件如 001_GET_path.req + 同名 .resp（可用子目录）；"
            "req 首行必须是 `METHOD /path` 或 `METHOD https://...`，"
            "resp 首行含状态码（如 HTTP/1.1 200）。"
            "确认后立即 create_vulnerability_report 并 finish_scan。"
        ),
    ]
    if minutes:
        os.environ["BEN_SCAN_MINUTES"] = str(minutes)
    elif profile.minutes:
        os.environ["BEN_SCAN_MINUTES"] = str(profile.minutes)

    box: dict[str, Any] = {
        "exit_code": None,
        "run_name": None,
        "stopped_reason": "",
        "stopped_code": StoppedCode.UNKNOWN,
        "kb_evidence": {},
    }

    def capture(args: Any) -> int:
        result = _execute_task(args)
        box["exit_code"] = result.exit_code
        box["run_name"] = result.run_name
        box["stopped_reason"] = result.stopped_reason
        box["stopped_code"] = coerce_stopped_code(result.stopped_code)
        if result.run_name:
            from Ben.core.paths import resolve_run_dir

            kb_file = resolve_run_dir(str(result.run_name)) / "kb_evidence.json"
            if kb_file.is_file():
                try:
                    box["kb_evidence"] = json.loads(kb_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
        return result.exit_code

    run_vuln(argv, run_task=capture)
    return ScanOutcome(
        stopped_code=coerce_stopped_code(box["stopped_code"]),
        stopped_reason=str(box["stopped_reason"] or ""),
        run_name=box["run_name"],
        exit_code=box["exit_code"],
        kb_evidence=dict(box.get("kb_evidence") or {}),
    )
