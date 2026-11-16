"""启动自检：端点拒绝 OpenAI 时不再使用 OpenAI Responses 客户端。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from Ben.config import llm_probe
from Ben.config.grok_build import GrokBuildPrivacyError
from Ben.config.settings import Settings
from Ben.interface.cli.scan_setup import preflight_model_connection


def test_unsupported_markers_and_missing_endpoints() -> None:
    assert llm_probe.is_openai_unsupported(RuntimeError("streaming is not supported"))
    assert llm_probe.is_openai_unsupported(_Status(404, "missing"))
    assert not llm_probe.is_openai_unsupported(RuntimeError("Connection error."))
    assert not llm_probe.is_openai_unsupported(_Status(401, "invalid api key"))
    assert not llm_probe.is_openai_unsupported(GrokBuildPrivacyError("grok-4.6"))


def test_preflight_drops_openai_when_responses_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_probe.reset()
    built: list[str] = []

    class _Fail:
        async def get_response(self, **_: Any) -> None:
            raise _Status(404, "unknown url /v1/responses")

    class _Ok:
        async def get_response(self, **_: Any) -> None:
            return None

    class _Provider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # 预检现按节点传凭据覆盖
            pass
        def get_model(self, name: str) -> Any:
            built.append(name)
            return _Fail() if len(built) == 1 else _Ok()

    monkeypatch.setattr("Ben.config.models.BenProvider", _Provider)
    monkeypatch.setattr("Ben.config.models.configure_sdk_model_defaults", lambda _settings: None)
    settings = Settings()
    settings.llm.model = "grok/grok-4.6"

    asyncio.run(preflight_model_connection("grok/grok-4.6", settings=settings))

    assert built == ["grok/grok-4.6", "grok/grok-4.6"]
    assert llm_probe.skip_openai() is True
    assert llm_probe.was_probed("grok/grok-4.6")
    llm_probe.reset()


def test_preflight_reuse_skips_a_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    llm_probe.reset()
    calls: list[str] = []

    class _Ok:
        async def get_response(self, **_: Any) -> None:
            calls.append("sent")

    class _Provider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # 预检现按节点传凭据覆盖
            pass
        def get_model(self, name: str) -> Any:
            return _Ok()

    monkeypatch.setattr("Ben.config.models.BenProvider", _Provider)
    monkeypatch.setattr("Ben.config.models.configure_sdk_model_defaults", lambda _settings: None)
    settings = Settings()
    settings.llm.model = "grok/grok-4.6"

    asyncio.run(preflight_model_connection("grok/grok-4.6", settings=settings))
    asyncio.run(preflight_model_connection("grok/grok-4.6", settings=settings, reuse=True))
    assert calls == ["sent"]
    llm_probe.reset()


def test_non_grok_unsupported_does_not_keep_using_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    llm_probe.reset()

    class _Fail:
        async def get_response(self, **_: Any) -> None:
            raise RuntimeError("streaming is not supported by this endpoint")

    class _Provider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # 预检现按节点传凭据覆盖
            pass
        def get_model(self, name: str) -> Any:
            return _Fail()

    monkeypatch.setattr("Ben.config.models.BenProvider", _Provider)
    monkeypatch.setattr("Ben.config.models.configure_sdk_model_defaults", lambda _settings: None)
    settings = Settings()
    settings.llm.model = "openai/gpt-4o"

    with pytest.raises(RuntimeError, match="已停止使用 OpenAI 客户端"):
        asyncio.run(preflight_model_connection("openai/gpt-4o", settings=settings))
    assert llm_probe.skip_openai() is False
    llm_probe.reset()


class _Status(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
