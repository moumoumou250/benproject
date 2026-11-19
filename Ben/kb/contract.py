"""三命令共享的退出码 / 子原因 / 结果类型。

CLI、JSON、测试全部引用这里的常量，禁止散落字面量 0/1/2/3
（主 CLI 的 ``--expect`` 用 3、``--fail-on`` 用 2，两套码位已共存）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    NEGATIVE = 1
    USAGE = 2
    INCONCLUSIVE = 3


class SubReason(StrEnum):
    """verify 子原因；check/diagnose 侧主要用 Attribution / 人读文案。"""

    BLOCKED = "BLOCKED"
    MISS_SURFACE = "MISS_SURFACE"
    TECHNIQUE_MISS = "TECHNIQUE_MISS"
    FALSE_NEGATIVE = "FALSE_NEGATIVE"
    NOT_VULNERABLE = "NOT_VULNERABLE"
    NO_VALIDATION = "NO_VALIDATION"
    UNREADABLE = "UNREADABLE"
    BUDGET = "BUDGET"
    CONNECTION_LOST = "CONNECTION_LOST"
    ORACLE_FAILED = "ORACLE_FAILED"


# 历史别名：旧稿 PAYLOAD_MISMATCH 并入 TECHNIQUE_MISS
PAYLOAD_MISMATCH = SubReason.TECHNIQUE_MISS


class Attribution(StrEnum):
    """diagnose 退出码语义（与 ExitCode 数值对齐，含义不同）。"""

    NAMING_GAP = "naming_gap"  # 0
    CONTENT_GAP = "content_gap"  # 1
    USAGE = "usage"  # 2
    UNATTRIBUTABLE = "unattributable"  # 3


@dataclass
class Result:
    """三命令统一结果；``code`` 决定进程退出码。"""

    code: ExitCode
    rows: list[Any] = field(default_factory=list)
    message: str = ""
    subreason: SubReason | None = None
    attribution: Attribution | None = None
    action: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": int(self.code),
            "message": self.message,
            "rows": self.rows,
        }
        if self.subreason is not None:
            payload["subreason"] = str(self.subreason)
        if self.attribution is not None:
            payload["attribution"] = str(self.attribution)
        if self.action is not None:
            payload["action"] = self.action
        if self.extra:
            payload.update(self.extra)
        return payload
