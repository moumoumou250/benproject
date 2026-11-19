"""Skill KB 校验与验证（check / diagnose / verify）的公共入口。

本包只服务 ``ben skill`` 三命令与词表；不碰 ``Ben/rag/``。
"""


from __future__ import annotations

from Ben.kb.attribution import diagnose_term
from Ben.kb.inventory import check_term
from Ben.kb.verify import verify_term
from Ben.kb.vocab import ALIASES, audit, canonical

__all__ = [
    "ALIASES",
    "audit",
    "canonical",
    "check_term",
    "diagnose_term",
    "verify_term",
]
