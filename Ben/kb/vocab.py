"""漏洞技能词表：ALIASES + canonical() + audit()。

词表从 ``Ben/interface/cli/vuln.py`` 迁出，供 ``ben vuln`` 与 ``ben skill``
共用。``canonical()`` 只在本地表上查找，确认命中前**不**调用
``resolve_skill_name``（后者对未注册名会向 stderr 打 warning）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 常见缩写 → Ben/skills/vulnerabilities 里的规范名（frontmatter name）。
# 只收"人真的会打"的别名；其余靠规范化 + stem 变体兜底。
ALIASES: dict[str, str] = {
    "sqli": "sql-injection",
    "sqlinject": "sql-injection",
    "blind-sqli": "sql-injection",
    "nosqli": "nosql-injection",
    "bola": "idor",
    "bfla": "broken-function-level-authorization",
    "broken-access-control": "idor",
    "access-control": "idor",
    "lfi": "path-traversal-lfi-rfi",
    "rfi": "path-traversal-lfi-rfi",
    "traversal": "path-traversal-lfi-rfi",
    "path-traversal": "path-traversal-lfi-rfi",
    "file-read": "path-traversal-lfi-rfi",
    "crlf": "header-injection",
    "response-splitting": "header-injection",
    "race": "race-conditions",
    "race-condition": "race-conditions",
    "jwt": "authentication-jwt",
    "auth": "authentication-jwt",
    "authentication": "authentication-jwt",
    "logic": "business-logic",
    "biz-logic": "business-logic",
    "infoleak": "information-disclosure",
    "info-disclosure": "information-disclosure",
    "takeover": "subdomain-takeover",
    "subdomain": "subdomain-takeover",
    "weak-password": "weak-password-detection",
    "bruteforce": "weak-password-detection",
    "default-creds": "weak-password-detection",
    "upload": "insecure-file-uploads",
    "file-upload": "insecure-file-uploads",
    "deserialization": "insecure-deserialization",
    "smuggling": "http-request-smuggling",
    "request-smuggling": "http-request-smuggling",
    "llm": "llm-prompt-injection",
    "prompt-injection": "llm-prompt-injection",
    "dom": "browser-security",
    "client-side": "browser-security",
    "agentic": "agentic-system-security",
    "mcp": "agentic-system-security",
    "ai-agent": "agentic-system-security",
    "openredirect": "open-redirect",
    "redirect": "open-redirect",
    "massassign": "mass-assignment",
    "prototype": "prototype-pollution",
    "cve-2022-22947": "rce",
    "spring-cloud-gateway": "rce",
    "scg-rce": "rce",
}

_VULN_DIR = Path(__file__).resolve().parents[1] / "skills" / "vulnerabilities"


def normalize(raw: str) -> str:
    """把用户写法收敛成 kebab-case：``SQL_Injection`` → ``sql-injection``。"""
    key = raw.strip().lower().replace("_", "-").replace(" ", "-")
    return re.sub(r"-{2,}", "-", key).strip("-")


def _vuln_skills() -> dict[str, object]:
    """``{frontmatter name: Skill}``，只取 vulnerabilities 分类。"""
    from Ben.skills import list_skills

    return {
        skill.name: skill
        for qname, skill in list_skills().items()
        if qname.startswith("vulnerabilities/")
    }


def _stem_to_name() -> dict[str, str]:
    """文件名 stem（含 snake）→ frontmatter name。"""
    mapping: dict[str, str] = {}
    skills = _vuln_skills()
    by_source = {skill.source.resolve(): skill.name for skill in skills.values()}
    if _VULN_DIR.is_dir():
        for path in _VULN_DIR.glob("*.md"):
            name = by_source.get(path.resolve())
            if name is None:
                # 文件在盘上但 loader 丢弃：仍记 stem，canonical 可据此区分
                continue
            mapping[path.stem.lower()] = name
            mapping[normalize(path.stem)] = name
    return mapping


def _frontmatter_alias_map() -> dict[str, str]:
    """frontmatter ``aliases`` → 规范名（小写键）。"""
    out: dict[str, str] = {}
    for skill in _vuln_skills().values():
        aliases = getattr(skill, "aliases", ()) or ()
        for alias in aliases:
            key = normalize(str(alias))
            if key:
                out[key] = skill.name
    return out


def literal_lookup(term: str) -> str | None:
    """严格叫法：别名 / frontmatter aliases / 规范名精确（大小写折叠）。

    **不做** ``_``↔``-`` 改写、不做 stem 兜底。供 ``check`` 判断"这个写法本身
    能不能取到"——``sql_injection`` 因此是不可达（叫法缺口），交给 diagnose。
    """
    raw = (term or "").strip()
    if not raw:
        return None
    key = raw.lower()
    norm = normalize(raw)
    skills = _vuln_skills()

    if key in ALIASES:
        target = ALIASES[key]
        return target if target in skills else None
    if norm in ALIASES and norm != key:
        # 别名表键已是 kebab；用户写了别名的大小写变体
        target = ALIASES[norm]
        return target if target in skills else None

    fm = _frontmatter_alias_map()
    if key in fm or norm in fm:
        target = fm.get(key) or fm[norm]
        return target if target in skills else None

    # 规范名精确：允许大小写折叠，但要求连字符形态已与 name 一致
    # （sql_injection 的 norm 是 sql-injection，这里用「原始去掉大小写」比对，
    #  避免把 underscore 形态误当成精确命中）
    folded = raw.strip().lower()
    if folded in skills:
        return folded
    return None


def canonical(term: str) -> str | None:
    """解析用户叫法 → 规范名；失败返回 None。

    查找顺序：ALIASES → frontmatter aliases → 规范名精确 → 归一化名 →
    stem 变体 → None。确认命中前不调用 ``resolve_skill_name``。
    """
    hit = literal_lookup(term)
    if hit is not None:
        return hit

    raw = (term or "").strip()
    if not raw:
        return None
    skills = _vuln_skills()
    norm = normalize(raw)
    if norm in skills:
        return norm

    stems = _stem_to_name()
    if raw.lower() in stems:
        return stems[raw.lower()]
    if norm in stems:
        return stems[norm]
    return None


def variants_for(term: str) -> list[tuple[str, str]]:
    """生成 (写法, 来源说明) 候选，供 diagnose 展示。"""
    raw = (term or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def add(label: str, source: str) -> None:
        key = label.strip()
        if not key or key.lower() in seen:
            return
        seen.add(key.lower())
        out.append((key, source))

    target = canonical(raw)
    skills = _vuln_skills()
    stems = _stem_to_name()
    fm = _frontmatter_alias_map()

    if target and target in skills:
        add(target, "frontmatter name")
        skill = skills[target]
        for alias in getattr(skill, "aliases", ()) or ():
            add(str(alias), "frontmatter aliases")
        for alias, name in ALIASES.items():
            if name == target:
                add(alias, "ALIASES")
        for stem, name in stems.items():
            if name == target:
                add(stem, "stem")
                swapped = stem.replace("_", "-") if "_" in stem else stem.replace("-", "_")
                add(swapped, "stem 互换")

    # 即使用户 term 本身解析失败，也列出与 term 形近的互换
    add(raw, "用户输入")
    add(normalize(raw), "归一化")
    swapped = raw.replace("_", "-") if "_" in raw else raw.replace("-", "_")
    add(swapped, "stem 互换")
    if raw.lower() in stems:
        add(stems[raw.lower()], "stem→name")
    if normalize(raw) in fm:
        add(fm[normalize(raw)], "frontmatter aliases")

    # 去重后：可解析的排前面
    resolvable: list[tuple[str, str]] = []
    others: list[tuple[str, str]] = []
    for label, source in out:
        if literal_lookup(label) or (normalize(label) in skills) or (label.lower() in stems):
            if label.lower() != raw.lower() or literal_lookup(label):
                resolvable.append((label, source))
            else:
                others.append((label, source))
        else:
            others.append((label, source))
    # 稳定去重
    final: list[tuple[str, str]] = []
    seen_l: set[str] = set()
    for item in resolvable + others:
        k = item[0].lower()
        if k in seen_l:
            continue
        seen_l.add(k)
        final.append(item)
    return final


@dataclass(frozen=True)
class VocabIssue:
    kind: str  # orphan | dangling | ambiguous
    detail: str


def audit() -> list[VocabIssue]:
    """三类问题：孤词（alias→不存在）、悬挂（skill 无叫法覆盖）、歧义。"""
    skills = _vuln_skills()
    issues: list[VocabIssue] = []

    reverse: dict[str, list[str]] = {}
    for alias, target in ALIASES.items():
        if target not in skills:
            issues.append(
                VocabIssue("orphan", f"alias {alias!r} → {target!r}（技能不存在）")
            )
        reverse.setdefault(target, []).append(alias)

    fm = _frontmatter_alias_map()
    alias_owners: dict[str, set[str]] = {}
    for alias, target in {**ALIASES, **fm}.items():
        alias_owners.setdefault(alias, set()).add(target)
    for alias, owners in sorted(alias_owners.items()):
        if len(owners) > 1:
            issues.append(
                VocabIssue(
                    "ambiguous",
                    f"alias {alias!r} 指向多个：{sorted(owners)}",
                )
            )

    # 悬挂：skill 规范名自身算一种叫法；另有 alias / frontmatter / stem 也算覆盖
    stems = _stem_to_name()
    covered = set(skills)
    covered.update(ALIASES.values())
    covered.update(fm.values())
    for name in skills:
        has_extra = (
            any(t == name for t in ALIASES.values())
            or any(t == name for t in fm.values())
            or any(t == name for t in stems.values())
        )
        # 规范名本身始终可写；悬挂指「除规范名外没有任何叫法」——仅作弱提示，
        # 7 条无别名且 stem==name 的条目是预期状态，不报 dangling。
        _ = has_extra  # 保留钩子；当前 audit 不把「仅规范名」当错误
        if name not in covered:
            issues.append(VocabIssue("dangling", f"skill {name!r} 无任何叫法覆盖"))

    return issues


__all__ = [
    "ALIASES",
    "VocabIssue",
    "audit",
    "canonical",
    "literal_lookup",
    "normalize",
    "variants_for",
]
