"""scan_clock：deadline / endgame / remaining（墙钟）。"""

from __future__ import annotations

import time

from Ben.core.scan_clock import (
    ENDGAME_FRACTION,
    SCAN_DEADLINE_KEY,
    install_deadline,
    is_deadline_passed,
    is_endgame,
    remaining_fraction,
    remaining_minutes,
)


def test_install_and_remaining() -> None:
    ctx: dict = {}
    assert install_deadline(ctx, minutes=10) == 10.0
    rem = remaining_minutes(ctx)
    assert rem is not None and 9.0 < rem <= 10.0
    assert remaining_fraction(ctx) is not None
    assert is_endgame(ctx) is False
    assert is_deadline_passed(ctx) is False


def test_endgame_and_deadline() -> None:
    ctx: dict = {}
    install_deadline(ctx, minutes=1)
    ctx[SCAN_DEADLINE_KEY] = time.time() - 1
    assert is_deadline_passed(ctx) is True
    install_deadline(ctx, minutes=10)
    ctx[SCAN_DEADLINE_KEY] = time.time() + (10 * 60 * ENDGAME_FRACTION * 0.5)
    assert is_endgame(ctx) is True


def test_unlimited_when_none() -> None:
    ctx: dict = {"other": 1}
    assert install_deadline(ctx, minutes=None) is None
    assert remaining_minutes(ctx) is None
    assert is_endgame(ctx) is False
