"""Backend bridge for external TUI clients."""

from Ben.interface.tui.backend.controller import TuiController
from Ben.interface.tui.backend.server import TuiBackendServer

__all__ = ["TuiBackendServer", "TuiController"]
