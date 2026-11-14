from types import SimpleNamespace

from Ben.config import codex
from Ben.config.settings import Settings
from Ben.interface.cli import environment


def test_subscription_settings_do_not_require_api_key():
    settings = Settings.model_construct(
        llm=SimpleNamespace(model="chatgpt/test-model", api_key="")
    )
    assert settings.validate() == []


def test_subscription_startup_requires_login_not_api_key(monkeypatch):
    settings = SimpleNamespace(llm=SimpleNamespace(model="chatgpt/test-model", api_key=""))
    monkeypatch.setattr(environment, "load_settings", lambda: settings)
    monkeypatch.setattr(codex, "is_authenticated", lambda: False)
    assert environment._missing_required_vars() == ["ChatGPT login: run ben auth login chatgpt"]
    monkeypatch.setattr(codex, "is_authenticated", lambda: True)
    assert environment._missing_required_vars() == []

