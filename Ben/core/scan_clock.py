"""Run 级 wall-clock：``BEN_SCAN_MINUTES`` / context deadline。

与轮数、美元预算并列；谁先触发谁收口。剩余 &lt;20% 时禁止新 surface 派发
（validator 与续领已有 claim 除外）。

已知限制：deadline 存墙钟 epoch（非 monotonic），resume 必须传入原始
``started_at``，否则会悄悄重置整段预算。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any


logger = logging.getLogger(__name__)

# 兼容旧键名；值语义改为 epoch 秒。
SCAN_DEADLINE_KEY = "scan_deadline_mono"
SCAN_STARTED_AT_KEY = "scan_started_at_epoch"
SCAN_MINUTES_KEY = "scan_minutes_total"
ENDGAME_FRACTION = 0.20


def resolve_scan_minutes() -> float | None:
    """从环境读总分钟数；空/未设 = 不限。"""
    raw = os.environ.get("BEN_SCAN_MINUTES", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("BEN_SCAN_MINUTES=%r invalid; ignoring", raw)
        return None
    if value <= 0:
        return None
    return value


def install_deadline(
    context: dict[str, Any],
    *,
    minutes: float | None = None,
    started_at: float | None = None,
) -> float | None:
    """写入 context；返回总分钟数（None=不限）。

    ``started_at`` 为墙钟 epoch；resume 时传入原 run 起点，避免时钟翻倍。
    """
    total = minutes if minutes is not None else resolve_scan_minutes()
    if total is None:
        context.pop(SCAN_DEADLINE_KEY, None)
        context.pop(SCAN_MINUTES_KEY, None)
        context.pop(SCAN_STARTED_AT_KEY, None)
        return None
    start = float(started_at) if started_at is not None else time.time()
    context[SCAN_MINUTES_KEY] = float(total)
    context[SCAN_STARTED_AT_KEY] = start
    context[SCAN_DEADLINE_KEY] = start + float(total) * 60.0
    return float(total)


def remaining_seconds(context: dict[str, Any] | None) -> float | None:
    if not context:
        return None
    deadline = context.get(SCAN_DEADLINE_KEY)
    if not isinstance(deadline, (int, float)):
        return None
    return max(0.0, float(deadline) - time.time())


def remaining_minutes(context: dict[str, Any] | None) -> float | None:
    secs = remaining_seconds(context)
    if secs is None:
        return None
    return secs / 60.0


def total_minutes(context: dict[str, Any] | None) -> float | None:
    if not context:
        return None
    total = context.get(SCAN_MINUTES_KEY)
    if isinstance(total, (int, float)) and total > 0:
        return float(total)
    return None


def remaining_fraction(context: dict[str, Any] | None) -> float | None:
    total = total_minutes(context)
    rem = remaining_minutes(context)
    if total is None or rem is None or total <= 0:
        return None
    return max(0.0, min(1.0, rem / total))


def is_endgame(context: dict[str, Any] | None) -> bool:
    frac = remaining_fraction(context)
    if frac is None:
        return False
    return frac < ENDGAME_FRACTION


def is_deadline_passed(context: dict[str, Any] | None) -> bool:
    secs = remaining_seconds(context)
    return secs is not None and secs <= 0


def time_status_line(context: dict[str, Any] | None) -> str | None:
    rem = remaining_minutes(context)
    total = total_minutes(context)
    if rem is None or total is None:
        return None
    pct = round(100 * (1.0 - (rem / total))) if total else 0
    return f"[TIME] remaining={rem:.1f}m / {total:g}m ({pct}% elapsed)"
