"""配置读取与校验（对齐 strix 的 pydantic-settings 模式）。

使用 pydantic-settings 的 BaseSettings 从 .env / 环境变量读取配置，
并提供 ``configure_sdk_model_defaults`` 把 LLM 配置注入 openai-agents SDK
的全局默认值（API Key、Base URL、API 路由），使 SDK 的 Runner 能直接
使用 DeepSeek/GLM 等 OpenAI 兼容端点。

环境变量命名保持与旧版兼容（DEEPSEEK_API_KEY / DEEPSEEK_MODEL / ...），
同时接受 strix 风格的别名（LLM_API_KEY / OPENAI_API_KEY 等）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（本文件位于 Ben/config/，故上溯两级）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_BASE_CONFIG = SettingsConfigDict(
    case_sensitive=False,
    populate_by_name=True,
    extra="ignore",
)


class LlmSettings(BaseSettings):
    """大模型配置（对应 strix 的 LlmSettings）。"""

    model_config = _BASE_CONFIG

    model: str = Field(
        default="deepseek-chat",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "LLM_MODEL", "STRIX_LLM"),
    )
    api_key: str = Field(
        default="",
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"),
        repr=False,
    )
    base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices(
            "DEEPSEEK_BASE_URL",
            "LLM_API_BASE",
            "OPENAI_API_BASE",
            "OPENAI_BASE_URL",
        ),
    )
    timeout: float = Field(
        default=60.0,
        validation_alias=AliasChoices("LLM_TIMEOUT", "DEEPSEEK_TIMEOUT"),
    )
    max_turns: int = Field(
        default=12,
        validation_alias=AliasChoices("MAX_AGENT_TURNS", "MAX_TURNS"),
    )

    @field_validator("api_key", "base_url", "model", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class Settings(BaseSettings):
    """运行配置（对应 strix 的 Settings）。"""

    model_config = _BASE_CONFIG

    # --- 大模型 ---
    llm: LlmSettings = Field(default_factory=LlmSettings)

    # 兼容属性：旧代码对 settings.deepseek_* 的直接访问
    @property
    def deepseek_api_key(self) -> str:
        return self.llm.api_key

    @property
    def deepseek_model(self) -> str:
        return self.llm.model

    @property
    def deepseek_base_url(self) -> str:
        return self.llm.base_url

    @property
    def llm_timeout(self) -> float:
        return self.llm.timeout

    @property
    def max_agent_turns(self) -> int:
        return self.llm.max_turns

    # --- 授权目标（白名单）---
    # 用 str 类型从环境变量读取，通过 property 转为 tuple。
    # pydantic-settings 对 tuple[str, ...] 类型会尝试 JSON 解析，
    # 而我们的 .env 使用逗号分隔字符串，所以先存为 raw 字符串再转换。
    allowed_targets_raw: str = Field(
        default="http://juice-shop:3000",
        validation_alias=AliasChoices("ALLOWED_TARGETS"),
    )

    @property
    def allowed_targets(self) -> tuple[str, ...]:
        """逗号分隔字符串 -> 元组（去空白、去空项、去尾部斜杠）。"""
        raw = self.allowed_targets_raw
        if not raw:
            return ()
        return tuple(
            item.strip().rstrip("/") for item in raw.split(",") if item.strip()
        )

    # --- Docker 执行器 ---
    executor_image: str = Field(
        default="ghcr.io/usestrix/strix-sandbox:1.3.0",
        validation_alias=AliasChoices("EXECUTOR_IMAGE"),
    )
    executor_network: str = Field(
        default="benptnet",
        validation_alias=AliasChoices("EXECUTOR_NETWORK"),
    )
    executor_container: str = Field(
        default="benpt-executor",
        validation_alias=AliasChoices("EXECUTOR_CONTAINER"),
    )
    command_timeout: float = Field(
        default=30.0,
        validation_alias=AliasChoices("COMMAND_TIMEOUT"),
    )

    # --- 工具输出上限（对齐 strix ContextSettings，供 output_store 截断/spill）---
    tool_output_max_lines: int = Field(
        default=2_000,
        gt=0,
        validation_alias=AliasChoices("TOOL_OUTPUT_MAX_LINES", "STRIX_TOOL_OUTPUT_MAX_LINES"),
    )
    # 下限高于截断通知本身的大小，保证预览一定放得下
    tool_output_max_bytes: int = Field(
        default=50 * 1024,
        ge=1024,
        validation_alias=AliasChoices("TOOL_OUTPUT_MAX_BYTES", "STRIX_TOOL_OUTPUT_MAX_BYTES"),
    )

    # --- 输出 ---
    reports_dir: Path = Field(default=PROJECT_ROOT / "reports")
    prompts_dir: Path = Field(
        default=Path(__file__).resolve().parents[1] / "agents" / "prompts",
    )

    def validate(self) -> list[str]:
        """返回配置错误列表（空列表表示通过）。"""
        problems: list[str] = []
        if not self.llm.api_key:
            problems.append("缺少 API Key：请复制 .env.example 为 .env 并填写 DEEPSEEK_API_KEY")
        if not self.llm.model:
            problems.append("缺少 DEEPSEEK_MODEL")
        if not self.llm.base_url:
            problems.append("缺少 DEEPSEEK_BASE_URL")
        if not self.allowed_targets:
            problems.append("缺少 ALLOWED_TARGETS：至少配置一个已授权目标")
        if self.llm.max_turns < 1:
            problems.append("MAX_AGENT_TURNS 必须 >= 1")
        return problems


def load_settings(*, require_api_key: bool = True) -> Settings:
    """加载 .env 与环境变量，构造并校验配置。

    require_api_key=False 时不强制要求 API Key（用于仅验证 Docker 的场景）。
    """
    # 加载项目根 .env（存在才加载），不覆盖已存在的真实环境变量。
    from dotenv import load_dotenv

    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    settings = Settings()

    problems = settings.validate()
    if not require_api_key:
        problems = [p for p in problems if "API Key" not in p]
    if problems:
        joined = "\n  - ".join(problems)
        raise SystemExit(f"配置校验失败：\n  - {joined}")
    return settings


def configure_sdk_model_defaults(settings: Settings) -> None:
    """把 Ben 配置注入 openai-agents SDK 的全局默认值。

    对齐 strix 的 ``configure_sdk_model_defaults``：设置 API Key、Base URL，
    指定使用 chat_completions API 路由（DeepSeek/GLM 等 OpenAI 兼容端点
    不支持 responses API），并禁用 SDK 遥测。
    """
    from agents import (
        set_default_openai_api,
        set_default_openai_key,
        set_tracing_disabled,
    )

    llm = settings.llm

    # 禁用 SDK 遥测（不向 OpenAI 发送追踪数据）
    set_tracing_disabled(True)

    # 设置 API Key
    if llm.api_key:
        set_default_openai_key(llm.api_key, use_for_tracing=False)

    # 设置 Base URL（SDK 通过环境变量读取）
    if llm.base_url:
        os.environ["OPENAI_BASE_URL"] = llm.base_url

    # DeepSeek/GLM 等 OpenAI 兼容端点只支持 chat_completions API
    set_default_openai_api("chat_completions")
