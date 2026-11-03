"""Path helpers that survive packaging (PyInstaller-style bundles)."""

from __future__ import annotations

import sys
from pathlib import Path


def get_ben_resource_path(*parts: str) -> Path:
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        base = Path(frozen_base) / "Ben"
        if base.exists():
            return base.joinpath(*parts)

    base = Path(__file__).resolve().parent.parent
    return base.joinpath(*parts)
