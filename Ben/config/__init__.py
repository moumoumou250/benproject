"""Ben.config：应用配置（导出面对齐 strix 的 config/__init__.py）。

- ``settings.py``：pydantic-settings 子模型结构（对齐 strix），含 Ben 扁平扩展；
- ``loader.py``：memoized 的 :func:`load_settings`（env > JSON 配置文件 > 默认值），
  以及 :func:`apply_config_override` / :func:`persist_current`；
- ``models.py``：模型路由/能力判断与 :func:`configure_sdk_model_defaults`
  （strix 版：含 request_log 安装、litellm 兼容、API 路由判定）。
"""

from typing import Any

from Ben.config.loader import apply_config_override, load_settings, persist_current
from Ben.config.settings import (
    ContextSettings,
    DedupeSettings,
    IntegrationSettings,
    LlmSettings,
    RuntimeSettings,
    Settings,
    TelemetrySettings,
    ViewerSettings,
)

__all__ = [
    "ContextSettings",
    "DedupeSettings",
    "IntegrationSettings",
    "LlmSettings",
    "RuntimeSettings",
    "Settings",
    "TelemetrySettings",
    "ViewerSettings",
    "apply_config_override",
    "configure_sdk_model_defaults",
    "load_settings",
    "persist_current",
]


def __getattr__(name: str) -> Any:
    """``models`` 会拉入整个 agents SDK，按需解析。

    ``Ben.report`` 等轻量模块只用到 ``loader``/``settings``，导入本包时
    不应顺带进入 SDK 的导入图（否则会和 import warm-up 线程抢导入锁）。
    """
    if name in ("configure_sdk_model_defaults", "BenProvider"):
        from Ben.config import models

        return getattr(models, name)
    raise AttributeError(name)
