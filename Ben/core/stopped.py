"""扫描结束原因的结构化码（唯一真相）。

``stopped_reason`` 仍是给人看的中文；``stopped_code`` 供 verify / 预算逻辑消费。
禁止在 CLI 与 kb 各写一份正则猜码。
"""

from __future__ import annotations

from enum import StrEnum


class StoppedCode(StrEnum):
    FINISHED = "finished"
    MAX_TURNS = "max_turns"
    BUDGET = "budget"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    STALLED = "stalled"
    UNKNOWN = "unknown"


def coerce_stopped_code(value: str | StoppedCode | None) -> StoppedCode:
    if value is None or value == "":
        return StoppedCode.UNKNOWN
    if isinstance(value, StoppedCode):
        return value
    try:
        return StoppedCode(str(value))
    except ValueError:
        return StoppedCode.UNKNOWN


# 遗留 reason→code 对照表（只允许这里维护；禁止「任意非空 → finished」）
_REASON_TABLE: tuple[tuple[tuple[str, ...], StoppedCode], ...] = (
    (("预算", "budget"), StoppedCode.BUDGET),
    (("轮数", "max turns", "max_turns"), StoppedCode.MAX_TURNS),
    (("finish_scan", "完成扫描"), StoppedCode.FINISHED),
    (("stalled", "心跳"), StoppedCode.STALLED),
    (("中断", "interrupt"), StoppedCode.INTERRUPTED),
    (("错误", "error", "fail", "crash"), StoppedCode.ERROR),
    (("无进展",), StoppedCode.FINISHED),
)


def stopped_code_from_reason(reason: str) -> StoppedCode:
    """仅作遗留兜底：优先使用 runner 直接写入的 ``stopped_code``。

    未命中对照表时返回 ``UNKNOWN``，不再把任意非空 reason 抬成 ``FINISHED``。
    """
    text = reason or ""
    low = text.lower()
    for needles, code in _REASON_TABLE:
        for n in needles:
            if n.lower() in low or n in text:
                return code
    return StoppedCode.UNKNOWN
