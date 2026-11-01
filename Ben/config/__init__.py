"""Ben.config：应用配置。

- ``settings.py``：从 .env / 环境变量读取并校验配置（对应 strix 的 config/settings.py）；
- 对外暴露 :class:`Settings` 、 :func:`load_settings` 与 :func:`configure_sdk_model_defaults`。
"""

from Ben.config.settings import Settings, configure_sdk_model_defaults, load_settings

__all__ = ["Settings", "load_settings", "configure_sdk_model_defaults"]
