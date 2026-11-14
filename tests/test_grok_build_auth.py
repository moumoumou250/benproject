"""Grok Build login reuse: model slug, token refresh, and startup requirements."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from openai import AsyncOpenAI

from Ben.config import grok_build
from Ben.config.models import BenProvider, _GrokResponsesModel, _TurnGuardModel
from Ben.config.settings import Settings
from Ben.interface.cli import environment
from Ben.llm import context_budget, request_log


def test_subscription_model_names() -> None:
    assert grok_build.subscription_model("grok") == "grok-4.6"
    assert grok_build.subscription_model("grok/grok-4.6") == "grok-4.6"
    assert grok_build.subscription_model("  Grok/grok-4.5  ") == "grok-4.5"
    assert grok_build.subscription_model("grok/") is None
    assert grok_build.subscription_model("xai/grok-4.7") is None
    assert grok_build.subscription_model(None) is None


def test_auth_mode_treats_grok_as_subscription() -> None:
    from Ben.config import codex

    assert codex.auth_mode("grok/grok-4.7") == "subscription"
    assert codex.auth_mode("grok") == "subscription"
    assert codex.auth_mode("xai/grok-4") == "api_key"


def test_parse_expiry_truncates_nanoseconds() -> None:
    parsed = grok_build._parse_expiry("2026-11-14T05:12:10.029392207Z")
    assert parsed is not None
    rendered = datetime.fromtimestamp(parsed, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert rendered == "2026-11-14T05:12:10.029392Z"


def test_settings_do_not_require_an_api_key() -> None:
    settings = Settings.model_construct(llm=SimpleNamespace(model="grok/grok-4.7", api_key=""))
    assert settings.validate() == []


def test_startup_requires_grok_login_not_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(llm=SimpleNamespace(model="grok", api_key=""))
    monkeypatch.setattr(environment, "load_settings", lambda: settings)
    monkeypatch.setattr(grok_build, "is_authenticated", lambda: False)
    assert environment._missing_required_vars() == ["Grok Build login: run grok login"]
    monkeypatch.setattr(grok_build, "is_authenticated", lambda: True)
    assert environment._missing_required_vars() == []


def test_refresh_rewrites_only_the_selected_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    auth = tmp_path / "auth.json"
    store = {
        "https://auth.x.ai::client": {
            "key": "old-access",
            "refresh_token": "old-refresh",
            "oidc_client_id": "client",
            "oidc_issuer": "https://auth.x.ai",
            "expires_at": "2000-01-01T00:00:00.000000Z",
            "email": "keep-me",
        },
        "other": {"untouched": True},
    }
    auth.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(grok_build, "AUTH_PATH", auth)

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 60}

    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: _Response())

    token = grok_build.get_valid_token()
    assert token == "new-access"
    written = json.loads(auth.read_text(encoding="utf-8"))
    assert written["other"] == {"untouched": True}
    record = written["https://auth.x.ai::client"]
    assert record["key"] == "new-access"
    assert record["refresh_token"] == "new-refresh"
    assert record["email"] == "keep-me"
    assert record["expires_at"] != "2000-01-01T00:00:00.000000Z"


def test_fresh_token_is_not_refreshed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "https://auth.x.ai::client": {
                    "key": "live-access",
                    "refresh_token": "refresh",
                    "oidc_client_id": "client",
                    "oidc_issuer": "https://auth.x.ai",
                    "expires_at": "2099-01-01T00:00:00.000000Z",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(grok_build, "AUTH_PATH", auth)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("refresh should not run")

    monkeypatch.setattr(requests, "post", _boom)
    assert grok_build.get_valid_token() == "live-access"


def test_client_version_comes_from_the_cli_install(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_file = tmp_path / "version.json"
    version_file.write_text('{"version": "1.2.3"}', encoding="utf-8")
    monkeypatch.setattr(grok_build, "VERSION_PATH", version_file)
    headers = grok_build.client_headers()
    assert headers["x-grok-client-version"] == "1.2.3"
    assert headers["User-Agent"] == "grok-cli/1.2.3"


def test_privacy_error_is_recognized() -> None:
    exc = RuntimeError(
        '403 {"error":"Grok Build is not supported in Legacy (no storage). [WKE=privacy-mode:legacy]"}'
    )
    assert grok_build.is_privacy_mode_error(exc)
    mapped = grok_build.GrokBuildPrivacyError("grok-4.7", exc)
    assert "Privacy Mode (no training)" in str(mapped)


def test_get_model_uses_grok_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    import Ben.config.loader as loader

    monkeypatch.setattr(grok_build, "get_client", lambda: AsyncOpenAI(api_key="x"))
    monkeypatch.setenv("LLM_MODEL", "grok/grok-4.7")
    monkeypatch.setenv("LLM_DISABLE_STREAMING", "true")
    loader._cached = None
    loader.load_settings()
    try:
        model = BenProvider().get_model("grok/grok-4.7")
        assert isinstance(model, _TurnGuardModel)
        logged = model._inner
        assert isinstance(logged, request_log.RequestLoggingModel)
        assert isinstance(logged._inner, _GrokResponsesModel)
    finally:
        loader._cached = None


def test_context_window_grok_prefix_skips_provider_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    context_budget._model_info.cache_clear()
    calls: list[str] = []

    def _model_info(model: str) -> dict[str, int]:
        calls.append(model)
        return {"max_input_tokens": 256_000, "max_output_tokens": 32_000}

    monkeypatch.setattr("litellm.get_model_info", _model_info)
    try:
        assert context_budget.context_window("grok/grok-4.7") == 256_000
        assert calls == ["grok-4.7"]
    finally:
        context_budget._model_info.cache_clear()
