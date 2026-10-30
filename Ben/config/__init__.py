"""Ben.config：应用配置。

- ``settings.py``：从 .env / 环境变量读取并校验配置（对应 strix 的 config/settings.py）；
- 对外只暴露 :class:`Settings` 与 :func:`load_settings`。
"""

from Ben.config.settings import Settings, load_settings

__all__ = ["Settings", "load_settings"]
