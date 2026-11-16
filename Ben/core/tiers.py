"""Agent tier 单一来源（create_agent / spawn / 并发闸门共用）。"""

from __future__ import annotations

from typing import Literal, get_args


AgentTier = Literal["scout", "hunter", "validator", "general"]
VALID_TIERS: tuple[str, ...] = get_args(AgentTier)
CLAIM_REQUIRED_TIERS: frozenset[str] = frozenset({"hunter", "general"})
