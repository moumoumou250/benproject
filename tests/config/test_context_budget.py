"""Tests for model-aware token budgets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from Ben.config import load_settings
from Ben.llm import context_budget


if TYPE_CHECKING:
    import pytest


def test_context_window_known_model() -> None:
    # gpt-4o is mapped by LiteLLM at 128k input tokens.
    assert context_budget.context_window("gpt-4o") == 128_000


def test_context_window_strips_provider_prefix() -> None:
    assert context_budget.context_window("openai/gpt-4o") == 128_000


def test_context_window_chatgpt_prefix_skips_provider_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_budget._model_info.cache_clear()
    calls: list[str] = []

    def _model_info(model: str) -> dict[str, int]:
        calls.append(model)
        return {"max_input_tokens": 1_050_000, "max_output_tokens": 128_000}

    monkeypatch.setattr("litellm.get_model_info", _model_info)
    try:
        assert context_budget.context_window("chatgpt/gpt-5.6-luna") == 1_050_000
        assert calls == ["gpt-5.6-luna"]
    finally:
        context_budget._model_info.cache_clear()


def test_context_window_unmapped_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    context_budget._model_info.cache_clear()

    def _raise(_model: str) -> dict[str, int]:
        raise ValueError("This model isn't mapped yet.")

    monkeypatch.setattr("litellm.get_model_info", _raise)
    expected = load_settings().context.fallback_context_tokens
    assert context_budget.context_window("totally-made-up-model") == expected
    context_budget._model_info.cache_clear()


def test_count_tokens_fallback_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**_kwargs: object) -> int:
        raise RuntimeError("no tokenizer")

    monkeypatch.setattr("litellm.token_counter", _raise)
    # Falls back to CJK-aware estimate: CJK ≈ 1 tok/char, else ≈ 4 chars/tok.
    assert context_budget.count_tokens("weird-model", "x" * 400) == 100
    assert context_budget.count_tokens("weird-model", "😀" * 10) == 3
    assert context_budget.count_tokens("weird-model", "测" * 10) == 10


def test_count_tokens_empty_is_zero() -> None:
    assert context_budget.count_tokens("gpt-4o", "") == 0


def test_approx_tokens_mixes_cjk_and_ascii() -> None:
    # 2 CJK + 4 ASCII → 2 + (4+3)//4 = 3
    assert context_budget._approx_tokens("测试abcd") == 3
