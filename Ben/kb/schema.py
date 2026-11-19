"""L0/L1 结构校验：返回缺失项列表，不抛异常。"""

from __future__ import annotations

import re

from Ben.kb.criteria import extract

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


def structure_ok(body: str) -> tuple[bool, list[str]]:
    """``--strict`` 要求：Validation ≥1 条可抽取条目，且存在 False Positives 段。"""
    missing: list[str] = []
    headings = {m.group(1).strip().lower() for m in _HEADING_RE.finditer(body or "")}
    if "validation" not in headings:
        missing.append("缺 ## Validation")
    else:
        criteria = extract(body or "", "Validation")
        if not criteria:
            missing.append("## Validation 为空")
    if "false positives" not in headings:
        missing.append("缺 ## False Positives")
    return (not missing, missing)
