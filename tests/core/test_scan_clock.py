"""scan_clock：deadline / endgame / remaining。"""

from __future__ import annotations

import time

from Ben.core.scan_clock import (
    ENDGAME_FRACTION,
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
    # tiny budget already mostly spent
    install_deadline(ctx, minutes=1)
    # force deadline into the past
    from Ben.core import scan_clock

    ctx[scan_clock.SCAN_DEADLINE_KEY] = time.monotonic() - 1
    assert is_deadline_passed(ctx) is True
    # endgame: set deadline so remaining fraction < 20%
    install_deadline(ctx, minutes=10)
    ctx[scan_clock.SCAN_DEADLINE_KEY] = time.monotonic() + (10 * 60 * ENDGAME_FRACTION * 0.5)
    assert is_endgame(ctx) is True


def test_unlimited_when_none() -> None:
    ctx: dict = {"other": 1}
    assert install_deadline(ctx, minutes=None) is None
    assert remaining_minutes(ctx) is None
    assert is_endgame(ctx) is False
