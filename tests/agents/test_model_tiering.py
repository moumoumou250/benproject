"""模型分层（root 旗舰档 + 子 agent flash 档）的单元测试。

覆盖三件事：
1. ``BEN_FLASH_MODEL`` 配置解析（settings 字段 + build_flash_model）；
2. build_agent / make_child_factory 的每-agent 模型覆盖（Agent.model）；
3. usage hooks 按 agent 实际模型名记账（flash 档不能按旗舰档单价算）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from Ben.config import load_settings
from Ben.config.models import build_flash_model
from tests.conftest import _STUB_SESSION, build_agent


# ---- 配置解析 ----------------------------------------------------------------


def test_flash_fields_default_to_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("BEN_FLASH_MODEL", "LLM_FLASH_MODEL", "BEN_FLASH_API_KEY", "BEN_FLASH_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings()
    assert settings.llm.flash_model is None
    assert settings.llm.flash_api_key == ""
    assert settings.llm.flash_api_base is None


def test_flash_fields_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEN_FLASH_MODEL", "deepseek/deepseek-chat")
    monkeypatch.setenv("BEN_FLASH_API_KEY", "sk-test")
    monkeypatch.setenv("BEN_FLASH_API_BASE", "https://gateway.example/v1")
    settings = load_settings()
    assert settings.llm.flash_model == "deepseek/deepseek-chat"
    assert settings.llm.flash_api_key == "sk-test"
    assert settings.llm.flash_api_base == "https://gateway.example/v1"


# ---- build_flash_model -------------------------------------------------------


def test_build_flash_model_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEN_FLASH_MODEL", raising=False)
    monkeypatch.delenv("LLM_FLASH_MODEL", raising=False)
    assert build_flash_model(load_settings()) is None


def test_build_flash_model_resolves_prefixed_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """前缀名走 LiteLLM 路由；构造不发网络请求，只验证返回了模型对象。"""
    monkeypatch.setenv("BEN_FLASH_MODEL", "deepseek/deepseek-chat")
    flash = build_flash_model(load_settings())
    assert flash is not None
    # BenProvider.get_model 的产物最外层是 RequestLoggingModel（带请求日志），
    # 其 model 属性是解析后的模型名——usage hooks 按它记账。
    assert getattr(flash, "model", None) == "deepseek/deepseek-chat"


# ---- build_agent / make_child_factory 的模型覆盖 ------------------------------


def test_build_agent_without_override_keeps_run_level_model() -> None:
    agent = build_agent(is_root=True)
    assert agent.model is None  # None = 沿用 RunConfig.model（旗舰档）


def test_build_agent_model_override_string() -> None:
    agent = build_agent(is_root=False, model_name="flash-model")
    assert agent.model == "flash-model"


def test_build_agent_model_override_model_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """runner 会把 build_flash_model 的 Model 实例直接传给子 agent 工厂。"""
    monkeypatch.setenv("BEN_FLASH_MODEL", "deepseek/deepseek-chat")
    flash = build_flash_model(load_settings())
    assert flash is not None
    agent = build_agent(is_root=False, model_name=flash)
    assert agent.model is flash


def test_child_factory_passes_model_through() -> None:
    from Ben.agents.factory import make_child_factory

    factory = make_child_factory(sandbox_session=_STUB_SESSION, model_name="flash-model")
    child = factory(name="scout", skills=[])
    assert child.model == "flash-model"


def test_child_factory_without_override_uses_run_level_model() -> None:
    from Ben.agents.factory import make_child_factory

    factory = make_child_factory(sandbox_session=_STUB_SESSION)
    child = factory(name="scout", skills=[])
    assert child.model is None


def test_child_factory_general_override_clears_model() -> None:
    """general 档：显式 model_name=None → 吃 RunConfig 旗舰。"""
    from Ben.agents.factory import make_child_factory

    factory = make_child_factory(sandbox_session=_STUB_SESSION, model_name="flash-model")
    general = factory(name="general", skills=[], model_name=None, use_root_capabilities=True)
    assert general.model is None


# ---- usage hooks 按 agent 模型记账 --------------------------------------------


def test_agent_model_name_helpers() -> None:
    from Ben.core.hooks import _agent_model_name

    # Agent.model=None → None（hooks 回退到 run 级模型名）
    assert _agent_model_name(SimpleNamespace(model=None)) is None
    # 字符串模型名直接返回
    assert _agent_model_name(SimpleNamespace(model="flash-model")) == "flash-model"
    # Model 实例：RequestLoggingModel 暴露 .model 属性
    assert _agent_model_name(SimpleNamespace(model=SimpleNamespace(model="deepseek/x"))) == (
        "deepseek/x"
    )
