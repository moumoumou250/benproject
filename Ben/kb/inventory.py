"""三源清单：disk / loaded / vocab；实现 ``check``。"""

from __future__ import annotations

from pathlib import Path

from Ben.kb.contract import ExitCode, Result
from Ben.kb.schema import structure_ok
from Ben.kb.vocab import literal_lookup, normalize

_VULN_DIR = Path(__file__).resolve().parents[1] / "skills" / "vulnerabilities"


def disk_stems() -> set[str]:
    if not _VULN_DIR.is_dir():
        return set()
    return {p.stem for p in _VULN_DIR.glob("*.md")}


def loaded_names() -> set[str]:
    from Ben.skills import list_skills

    return {
        skill.name
        for qname, skill in list_skills().items()
        if qname.startswith("vulnerabilities/")
    }


def _file_for_name(name: str) -> Path | None:
    """按 frontmatter name 反查源文件。"""
    from Ben.skills import list_skills

    for qname, skill in list_skills().items():
        if qname == f"vulnerabilities/{name}":
            return skill.source
    return None


def _reason_unreachable(term: str, disk: set[str], loaded: set[str]) -> str:
    """一行原因，指向下一步该跑 diagnose。"""
    norm = normalize(term)
    stem_hit = term in disk or term.lower() in {s.lower() for s in disk}
    norm_stem = normalize(term) in {normalize(s) for s in disk}

    if stem_hit or norm_stem:
        # 文件在，但叫法不是规范名/别名
        matched_stem = next(
            (s for s in disk if s.lower() == term.lower() or normalize(s) == norm),
            term,
        )
        # 尝试读 frontmatter name
        path = _VULN_DIR / f"{matched_stem}.md"
        fm_name = None
        if path.is_file():
            from Ben.skills import list_skills

            for skill in list_skills().values():
                if skill.source.resolve() == path.resolve():
                    fm_name = skill.name
                    break
            if fm_name is None:
                return "loader 已丢弃（frontmatter 非法）"
        if fm_name and fm_name in loaded:
            return f"文件存在，frontmatter name={fm_name}（叫法缺口 → diagnose）"
        if fm_name:
            return "loader 已丢弃（frontmatter 非法）"
        return "文件存在但未能解析 frontmatter"

    if literal_lookup(term) is None and norm not in loaded:
        # 也可能是别名指向了丢失条目
        from Ben.kb.vocab import ALIASES

        alias_target = ALIASES.get(normalize(term)) or ALIASES.get(term.lower())
        if alias_target and alias_target not in loaded:
            if any(normalize(s) == normalize(alias_target) or s == alias_target for s in disk):
                return "loader 已丢弃（frontmatter 非法）"
            return "条目不存在"
        return "词表外（叫法缺口 → diagnose）"

    return "不可达"


def check_term(
    term: str | None = None,
    *,
    strict: bool = False,
    all_terms: bool = False,
) -> Result:
    """可达性校验。

    - 无 term / ``all_terms``：校验全部已载入条目
    - 有 term：用 ``literal_lookup``（不做 stem 静默改写）
    - ``strict``：追加 Validation / False Positives 结构要求
    """
    disk = disk_stems()
    loaded = loaded_names()

    if not _VULN_DIR.is_dir():
        return Result(
            ExitCode.USAGE,
            message="技能目录不可枚举：Ben/skills/vulnerabilities/",
        )

    if term and all_terms:
        return Result(
            ExitCode.USAGE,
            message="<term> 与 --all 互斥",
        )

    rows: list[dict[str, object]] = []

    if term is None or all_terms:
        for name in sorted(loaded):
            ok = True
            detail = ""
            if strict:
                path = _file_for_name(name)
                sok, missing = structure_ok(path.read_text(encoding="utf-8") if path else "")
                ok = sok
                detail = (
                    f"Validation / FP 合规"
                    if sok
                    else f"结构不合规：{', '.join(missing)}"
                )
            else:
                # 非 strict：附带判据条数提示（不参与退出码）
                path = _file_for_name(name)
                from Ben.kb.criteria import extract

                body = path.read_text(encoding="utf-8") if path else ""
                n_val = len(extract(body, "Validation"))
                n_fp = len(extract(body, "False Positives"))
                detail = f"Validation {n_val} 条 / FP {n_fp} 条"
            rows.append(
                {
                    "term": name,
                    "reachable": ok,
                    "detail": detail if ok else detail or "不可达",
                }
            )
        # strict 下结构失败 → 1；否则全员可达 → 0
        code = ExitCode.OK if all(bool(r["reachable"]) for r in rows) else ExitCode.NEGATIVE
        return Result(code, rows=rows)

    # 单 term
    target = literal_lookup(term)
    reachable = target is not None and target in loaded
    detail = ""
    if reachable:
        path = _file_for_name(target)  # type: ignore[arg-type]
        body = path.read_text(encoding="utf-8") if path else ""
        if strict:
            sok, missing = structure_ok(body)
            reachable = sok
            detail = (
                f"Validation / FP 合规"
                if sok
                else f"结构不合规：{', '.join(missing)}"
            )
        else:
            from Ben.kb.criteria import extract

            n_val = len(extract(body, "Validation"))
            n_fp = len(extract(body, "False Positives"))
            detail = f"Validation {n_val} 条 / FP {n_fp} 条"
    else:
        detail = _reason_unreachable(term, disk, loaded)

    rows.append({"term": term, "reachable": reachable, "detail": detail})
    return Result(
        ExitCode.OK if reachable else ExitCode.NEGATIVE,
        rows=rows,
        message=detail if not reachable else "",
    )
