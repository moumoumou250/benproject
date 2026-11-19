"""结果 → KB 动作类（§7.1 的唯一实现）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from Ben.kb.contract import ExitCode, SubReason


@dataclass(frozen=True)
class Action:
    class_: str  # A | B | C | D
    kb_file: str | None
    hint: str

    def to_dict(self) -> dict[str, Any]:
        return {"class": self.class_, "kb_file": self.kb_file, "hint": self.hint}


def recommend(
    *,
    check_code: ExitCode | int | None = None,
    verify_code: ExitCode | int | None = None,
    subreason: SubReason | str | None = None,
    has_bypass: bool = False,
    expects_vuln: bool = True,
    kb_file: str | None = None,
    strict: bool = False,  # noqa: ARG001 — 预留与 check --strict 对齐
) -> Action | None:
    """按 §7.1 映射；负样本（expects_vuln=False）对码 1 一律返回 None。"""
    vc = int(verify_code) if verify_code is not None else None
    cc = int(check_code) if check_code is not None else None
    sr = str(subreason) if subreason is not None else None

    if not expects_vuln and vc == int(ExitCode.NEGATIVE):
        return None

    # D：verify 2 词表外
    if vc == int(ExitCode.USAGE):
        return Action(
            "D",
            "Ben/kb/vocab.py",
            "扩词表：ALIASES 新增 term → 规范名",
        )

    # C：verify 0 且 check 1
    if vc == int(ExitCode.OK) and cc == int(ExitCode.NEGATIVE):
        return Action(
            "C",
            kb_file,
            "补别名：frontmatter aliases（零 prompt 影响）",
        )

    # A：UNREADABLE / NO_VALIDATION
    if vc == int(ExitCode.INCONCLUSIVE) and sr == SubReason.UNREADABLE:
        return Action("A", kb_file, "改判据：Validation 条目改成可观察断言")
    if vc == int(ExitCode.INCONCLUSIVE) and sr == SubReason.NO_VALIDATION:
        return Action("A", kb_file, "补判据：新增 ## Validation（≥3 条）+ ## False Positives")
    # diagnose 内容缺口（无 validation）也走 A
    if vc == int(ExitCode.INCONCLUSIVE) and sr is None and cc == int(ExitCode.OK):
        return Action("A", kb_file, "补判据：## Validation 缺失或为空")
    if vc == int(ExitCode.INCONCLUSIVE) and cc == int(ExitCode.NEGATIVE):
        return Action("A", kb_file, "修内容或 frontmatter（loader 丢弃/条目缺失）")

    # B：假阴性子原因
    if vc == int(ExitCode.NEGATIVE):
        if sr == SubReason.MISS_SURFACE:
            return Action("B", kb_file, "修侦察/入口：补 oracle 所在面")
        if sr == SubReason.TECHNIQUE_MISS:
            return Action("B", kb_file, "修手法：路径·参数·payload 对齐 oracle")
        if sr == SubReason.BLOCKED and not has_bypass:
            return Action("B", kb_file, "补绕过：## Bypass Techniques")
        if sr == SubReason.FALSE_NEGATIVE and cc == int(ExitCode.OK):
            return Action("B", kb_file, "修现有 skill 内容（禁止新建条目）")
        if sr == SubReason.BLOCKED and has_bypass:
            return None  # 有绕过节仍被拦 → 环境/靶场，不动 KB
        if sr is None:
            return Action("B", kb_file, "修现有 skill 内容")

    # 环境类 / 闭环：不动 KB
    return None
