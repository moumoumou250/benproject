"""启动交互式终端界面（TUI）的薄封装（移植自 strix 的 ``interface/interactive.py``）。

把 TUI 子进程的两种"起不来"形态翻译成入口层可以统一处理的异常：
- 激活前失败（没有终端 / 二进制缺失）→ :class:`InteractiveSetupUnavailableError`
- 激活后死亡（终端被杀 / 子进程退出）→ :class:`InteractiveInterfaceExitedError`

入口（:mod:`Ben.interface.main`）据此决定是否回退到 headless 模式。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import argparse


logger = logging.getLogger(__name__)

__all__ = [
    "InteractiveInterfaceExitedError",
    "InteractiveSetupUnavailableError",
    "run_tui",
]


class InteractiveSetupUnavailableError(RuntimeError):
    """交互式界面无法启动（无终端、sidecar 缺失等）。"""


class InteractiveInterfaceExitedError(RuntimeError):
    """交互式界面起来之后退出（终端被杀、子进程异常）。"""


async def run_tui(args: argparse.Namespace) -> None:
    """运行 Go 版 Bubble Tea TUI。"""
    from Ben.interface.tui.runtime import (
        GoTuiPreActivationError,
        run_go_tui,
    )
    from Ben.interface.tui.sidecar import TuiProcessExitedError

    try:
        await run_go_tui(args)
    except GoTuiPreActivationError as exc:
        raise InteractiveSetupUnavailableError(
            f"交互式界面无法启动：{exc}"
        ) from exc
    except TuiProcessExitedError as exc:
        raise InteractiveInterfaceExitedError(str(exc)) from exc
