"""从 skill 正文抽取 Validation / False Positives / Bypass* 条目。"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 段落标题白名单（必须，否则 Bypass Methods 条目会误触发「无绕过节」）
VALIDATION_HEADINGS = ("Validation",)
FALSE_POSITIVE_HEADINGS = ("False Positives",)
BYPASS_HEADINGS = ("Bypass Techniques", "Bypass Methods")

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_ITEM_RE = re.compile(r"^\s*(?:(\d+)[\.\)]\s+|[-*]\s+)(.+)$")

# 过程句 / 注意句：不当作可核对判据
_UNCHECKABLE = re.compile(
    r"^(?:provide|note|notes?|caution|avoid|remember|see also|refer)\b",
    re.I,
)
_HAS_VERB = re.compile(
    r"\b(?:demonstrate|show|confirm|verify|prove|observe|return|returns|"
    r"access|replace|compare|check|detect|identify|exploit|inject|"
    r"request|send|retrieve|read|write|execute|bypass|trigger)\b",
    re.I,
)


@dataclass(frozen=True)
class Criterion:
    index: int  # 段内序号（从 1 起）
    text: str  # 原文，一字不改
    kind: str  # numbered | bullet | prose
    checkable: bool


def _section_body(text: str, headings: tuple[str, ...]) -> str | None:
    """取出第一个匹配标题下的正文（到下一 ## 为止）。"""
    wanted = {h.lower() for h in headings}
    matches = list(_HEADING_RE.finditer(text or ""))
    for i, match in enumerate(matches):
        if match.group(1).strip().lower() in wanted:
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return text[start:end]
    return None


def extract(body: str, heading: str) -> list[Criterion]:
    """按标题抽取编号/项目符号条目；``heading`` 支持白名单别名。"""
    heading_map = {
        "validation": VALIDATION_HEADINGS,
        "false positives": FALSE_POSITIVE_HEADINGS,
        "bypass techniques": BYPASS_HEADINGS,
        "bypass methods": BYPASS_HEADINGS,
        "bypass": BYPASS_HEADINGS,
    }
    key = heading.strip().lower()
    aliases = heading_map.get(key, (heading,))
    section = _section_body(body, aliases)
    if section is None:
        return []

    items: list[Criterion] = []
    for line in section.splitlines():
        match = _ITEM_RE.match(line)
        if not match:
            continue
        text = match.group(2).strip()
        if not text:
            continue
        kind = "numbered" if match.group(1) else "bullet"
        index = len(items) + 1
        items.append(
            Criterion(
                index=index,
                text=text,
                kind=kind,
                checkable=is_checkable_text(text),
            )
        )
    return items


def is_checkable_text(text: str) -> bool:
    """L1 启发式：有动词 + 可观察对象，且非注意/过程句。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    if _UNCHECKABLE.match(cleaned):
        return False
    if not _HAS_VERB.search(cleaned):
        return False
    # 太短通常是标签而非断言
    return len(cleaned) >= 24


def is_checkable(c: Criterion) -> bool:
    return c.checkable


def has_bypass_section(body: str) -> bool:
    return _section_body(body, BYPASS_HEADINGS) is not None
