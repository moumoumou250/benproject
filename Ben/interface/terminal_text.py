"""Safe rendering of untrusted text in a terminal."""

from __future__ import annotations

import os
import re
import sys


_TERMINAL_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def has_terminal_control(value: object) -> bool:
    """Return whether text contains bytes that can alter terminal state/protocols."""
    return _TERMINAL_CONTROL.search(str(value)) is not None


def sanitize_terminal_text(value: object) -> str:
    """Make C0/C1 control bytes visible so they cannot operate a terminal."""
    return _TERMINAL_CONTROL.sub(
        lambda match: f"\\x{ord(match.group()):02x}",
        str(value),
    )


def terminal_attached() -> bool:
    """Whether the TUI can take over the terminal: a tty on both ends, not dumb."""
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    return all(hasattr(stream, "isatty") and stream.isatty() for stream in (sys.stdin, sys.stdout))
