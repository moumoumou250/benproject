"""KB / runner 共用的环境开关解析（禁止各写一套真值表）。"""

from __future__ import annotations

import os

_TRUE = frozenset({"1", "true", "TRUE", "yes", "YES", "on", "ON"})


def env_flag(name: str, *, default: bool = False) -> bool:
    """读 ``BEN_*`` 布尔开关；未设置时返回 ``default``。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = raw.strip()
    if not text:
        return default
    return text in _TRUE
