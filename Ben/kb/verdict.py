"""逐条判定 + confirm() 有序汇总（全仓库唯一构造 ExitCode.OK 的路径）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from Ben.kb.contract import ExitCode, SubReason
from Ben.kb.criteria import Criterion
from Ben.kb.evidence import Evidence, is_confirmable_kind


VerdictResult = Literal["PASS", "FAIL", "UNOBSERVED", "SKIPPED"]


@dataclass
class CriterionVerdict:
    index: int
    text: str
    checkable: bool
    required: bool
    result: VerdictResult
    by: str | None
    evidence_refs: list[str] = field(default_factory=list)
    passed: bool = False
    criterion_kind: str = "numbered"


@dataclass(frozen=True)
class ConfirmResult:
    code: ExitCode
    subreason: SubReason | None = None
    message: str = ""


def confirm(
    criteria: list[Criterion],
    verdicts: list[CriterionVerdict],
    evidence: list[Evidence],
    *,
    expects_vuln: bool,
    surface_hit: bool,
    blocked: bool,
) -> ConfirmResult:
    """有序判定；``Result(ExitCode.OK)`` 只允许从这里产生。"""
    kind_by_ref = {e.path: e.kind for e in evidence}
    if not criteria:
        return ConfirmResult(ExitCode.INCONCLUSIVE, SubReason.NO_VALIDATION, "无 validation")
    if blocked:
        return ConfirmResult(ExitCode.NEGATIVE, SubReason.BLOCKED, "拦截特征")
    if expects_vuln and not surface_hit:
        return ConfirmResult(ExitCode.NEGATIVE, SubReason.MISS_SURFACE, "未打到 oracle 面")
    required = [v for v in verdicts if v.required]
    if any(v.result == "UNOBSERVED" for v in required):
        return ConfirmResult(ExitCode.INCONCLUSIVE, SubReason.UNREADABLE, "必需判据未观察")
    if any(v.result == "FAIL" for v in required):
        return ConfirmResult(ExitCode.NEGATIVE, SubReason.FALSE_NEGATIVE, "必需判据 FAIL")
    if not required:
        checkable = [v for v in verdicts if v.checkable]
        if not checkable:
            return ConfirmResult(ExitCode.INCONCLUSIVE, SubReason.UNREADABLE, "无可核对判据")
        if any(v.result != "PASS" or not v.evidence_refs for v in checkable):
            return ConfirmResult(ExitCode.INCONCLUSIVE, SubReason.UNREADABLE, "判据证据不足")
        if not _refs_confirmable(checkable, kind_by_ref):
            return ConfirmResult(ExitCode.INCONCLUSIVE, SubReason.UNREADABLE, "仅有辅助证据")
        return ConfirmResult(ExitCode.OK, None, "CONFIRMED")
    if not all(v.passed and v.evidence_refs for v in required):
        return ConfirmResult(ExitCode.INCONCLUSIVE, SubReason.UNREADABLE, "必需判据证据不足")
    if not _refs_confirmable(required, kind_by_ref):
        return ConfirmResult(ExitCode.INCONCLUSIVE, SubReason.UNREADABLE, "仅有辅助证据")
    return ConfirmResult(ExitCode.OK, None, "CONFIRMED")


def _refs_confirmable(
    verdicts: list[CriterionVerdict], kind_by_ref: dict[str, Any]
) -> bool:
    for v in verdicts:
        for ref in v.evidence_refs:
            if ref.startswith("audit/") or "trace" in ref:
                return False
            kind = kind_by_ref.get(ref)
            if kind is None:
                # 未知 ref：按 path 启发式拒 e3 目录
                if ref.startswith("findings") or ref.startswith("findings_http/"):
                    return False
                continue
            if not is_confirmable_kind(kind):
                return False
    return True


def build_verdicts(
    criteria: list[Criterion],
    *,
    expected_indices: tuple[int, ...] | list[int],
    mechanical: dict[int, tuple[VerdictResult, list[str]]] | None = None,
) -> list[CriterionVerdict]:
    """组装逐条判定；``mechanical`` 提供 index → (result, evidence_refs)。"""
    mechanical = mechanical or {}
    expected = set(expected_indices)
    out: list[CriterionVerdict] = []
    for c in criteria:
        required = (c.index in expected) if expected else c.checkable
        if c.index in mechanical:
            result, refs = mechanical[c.index]
            out.append(
                CriterionVerdict(
                    index=c.index,
                    text=c.text,
                    checkable=True,
                    required=required,
                    result=result,
                    by="mechanical",
                    evidence_refs=list(refs),
                    passed=result == "PASS" and bool(refs),
                    criterion_kind=c.kind,
                )
            )
            continue
        if not c.checkable:
            out.append(
                CriterionVerdict(
                    index=c.index,
                    text=c.text,
                    checkable=False,
                    required=False,
                    result="SKIPPED",
                    by=None,
                    evidence_refs=[],
                    passed=False,
                    criterion_kind=c.kind,
                )
            )
            continue
        out.append(
            CriterionVerdict(
                index=c.index,
                text=c.text,
                checkable=True,
                required=required,
                result="UNOBSERVED",
                by=None,
                evidence_refs=[],
                passed=False,
                criterion_kind=c.kind,
            )
        )
    return out


def verdicts_to_json(verdicts: list[CriterionVerdict]) -> list[dict[str, Any]]:
    return [
        {
            "index": v.index,
            "text": v.text,
            "kind": v.criterion_kind,
            "checkable": v.checkable,
            "required": v.required,
            "result": v.result,
            "by": v.by,
            "evidence": v.evidence_refs,
        }
        for v in verdicts
    ]


_VERDICT_REQUIRED = frozenset(
    {
        "schema",
        "term",
        "resolved",
        "profile",
        "expects_vuln",
        "target",
        "code",
        "verdict",
        "evidence_dir",
    }
)


def validate_verdict_payload(payload: dict[str, Any]) -> list[str]:
    """轻量 schema 检查；返回错误列表（空=通过）。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload 不是 dict"]
    missing = sorted(_VERDICT_REQUIRED - set(payload))
    if missing:
        errors.append(f"缺字段: {', '.join(missing)}")
    if payload.get("schema") != "ben.kb.verdict/1":
        errors.append(f"schema 非法: {payload.get('schema')!r}")
    code = payload.get("code")
    if not isinstance(code, int) or code not in {0, 1, 2, 3}:
        errors.append(f"code 非法: {code!r}")
    if "criteria" in payload and not isinstance(payload["criteria"], list):
        errors.append("criteria 必须是 list")
    if "paths_seen" in payload and not isinstance(payload["paths_seen"], list):
        errors.append("paths_seen 必须是 list")
    if "paths_hint_db" in payload and not isinstance(payload["paths_hint_db"], list):
        errors.append("paths_hint_db 必须是 list")
    return errors
