"""diagnose：三源求差归因。"""

from __future__ import annotations

from pathlib import Path

from Ben.kb.actions import recommend
from Ben.kb.contract import Attribution, ExitCode, Result
from Ben.kb.criteria import extract, is_checkable
from Ben.kb.inventory import disk_stems, loaded_names
from Ben.kb.vocab import canonical, literal_lookup, normalize, variants_for

_VULN_DIR = Path(__file__).resolve().parents[1] / "skills" / "vulnerabilities"


def _skill_path(name: str) -> str | None:
    from Ben.skills import list_skills

    for skill in list_skills().values():
        if skill.name == name and skill.category == "vulnerabilities":
            return str(skill.source)
    return None


def _stem_path(term: str) -> str | None:
    for stem in disk_stems():
        if stem.lower() == term.lower() or normalize(stem) == normalize(term):
            return str(_VULN_DIR / f"{stem}.md")
    return None


def _resolvable_variants(
    term: str, extra: list[str] | None
) -> list[tuple[str, str]]:
    """返回「能解析到规范名」且不等于「字面即可达的原词」的变体列表。"""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, source in variants_for(term):
        hit = literal_lookup(label) or canonical(label)
        if not hit:
            continue
        key = label.lower()
        if key in seen:
            continue
        # 原词若字面可达，不算「变体」
        if key == term.lower() and literal_lookup(term) is not None:
            continue
        seen.add(key)
        rows.append((label, source))
    for v in extra or []:
        hit = literal_lookup(v) or canonical(v)
        if hit and v.lower() not in seen:
            seen.add(v.lower())
            rows.append((v, "--variant"))
    return rows


def diagnose_term(
    term: str,
    *,
    extra_variants: list[str] | None = None,
    strict_contract: bool = False,
) -> Result:
    """0 叫法缺口 / 1 内容缺口 / 2 用法 / 3 无法归因。"""
    raw = (term or "").strip()
    if not raw:
        return Result(
            ExitCode.USAGE,
            attribution=Attribution.USAGE,
            message="缺少 <term>",
        )

    loaded = loaded_names()
    disk = disk_stems()
    lit = literal_lookup(raw)
    canon = canonical(raw)
    variants = _resolvable_variants(raw, extra_variants)
    n_variants = len(variants)

    # —— 字面解析失败：叫法缺口（或 D3 用法）——
    if lit is None:
        # D3：term 解析失败且变体 < 2 → 2（仅 strict_contract 或变体为 0 且 canon 也无）
        if n_variants < 2 and canon is None:
            if strict_contract or n_variants < 1:
                return Result(
                    ExitCode.USAGE,
                    attribution=Attribution.USAGE,
                    message="用法配置错（变体不足且无法解析）",
                    rows=[{"term": raw, "variants": variants}],
                    action=recommend(
                        verify_code=ExitCode.USAGE,
                        check_code=ExitCode.NEGATIVE,
                    ).to_dict(),
                    extra={"variants": n_variants},
                )
        # 叫法缺口：变体能解析，或 canon 能通（normalize/stem）
        path = _skill_path(canon) if canon else _stem_path(raw)
        action = recommend(
            check_code=ExitCode.NEGATIVE,
            verify_code=ExitCode.OK if canon else ExitCode.USAGE,
            kb_file=path,
        )
        hints = [f"{lab}（{src}）" for lab, src in variants[:5]]
        if canon and canon.lower() not in {h.split("（", 1)[0].lower() for h in hints}:
            hints.insert(0, f"{canon}（canonical）")
        return Result(
            ExitCode.OK,
            attribution=Attribution.NAMING_GAP,
            message="叫法缺口",
            rows=[{"term": raw, "resolved": canon, "variants": variants, "file": path}],
            action=action.to_dict() if action else None,
            extra={
                "variants": max(n_variants, 1 if canon else 0),
                "hint": "变体可解析：" + "、".join(hints) if hints else "",
                "file": path,
            },
        )

    # —— 字面可解析 ——
    if lit not in loaded:
        stem_exists = any(
            normalize(s) == normalize(lit) or s.replace("_", "-") == lit for s in disk
        )
        kind = "loader 丢弃" if stem_exists else "条目不存在"
        path = _stem_path(lit) or _stem_path(raw)
        return Result(
            ExitCode.NEGATIVE,
            attribution=Attribution.CONTENT_GAP,
            message=f"内容缺口（{kind}）",
            rows=[{"term": raw, "resolved": lit, "in_loaded": False, "file": path}],
            action=recommend(
                check_code=ExitCode.NEGATIVE,
                verify_code=ExitCode.INCONCLUSIVE,
                kb_file=path,
            ).to_dict(),
            extra={"variants": n_variants, "file": path},
        )

    from Ben.skills import list_skills

    body = ""
    path = _skill_path(lit)
    for skill in list_skills().values():
        if skill.name == lit and skill.category == "vulnerabilities":
            body = skill.body
            break

    criteria = extract(body, "Validation")
    if not criteria:
        return Result(
            ExitCode.NEGATIVE,
            attribution=Attribution.CONTENT_GAP,
            message="内容缺口（## Validation 缺失或为空）",
            rows=[{"term": raw, "resolved": lit, "criteria": 0, "file": path}],
            action=recommend(
                check_code=ExitCode.OK,
                verify_code=ExitCode.INCONCLUSIVE,
                kb_file=path,
            ).to_dict(),
            extra={"variants": n_variants, "file": path},
        )

    checkable = [c for c in criteria if is_checkable(c)]
    if not checkable:
        return Result(
            ExitCode.INCONCLUSIVE,
            attribution=Attribution.UNATTRIBUTABLE,
            message="无法归因（判据均不可逐条核对）",
            rows=[
                {
                    "term": raw,
                    "resolved": lit,
                    "criteria": len(criteria),
                    "checkable": 0,
                    "file": path,
                }
            ],
            extra={"variants": n_variants, "file": path},
        )

    msg = "可达"
    if n_variants < 2:
        msg += "（该条目没有别名，反查能力有限）"
    return Result(
        ExitCode.OK,
        message=msg,
        rows=[
            {
                "term": raw,
                "resolved": lit,
                "criteria": len(criteria),
                "checkable": len(checkable),
                "file": path,
            }
        ],
        extra={"variants": n_variants, "file": path},
    )
