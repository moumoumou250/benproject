"""配置读取与校验（子模型结构对齐 strix 的 config/settings.py）。

LlmSettings / ContextSettings / DedupeSettings / TelemetrySettings 等子模型
字段与 strix 完全一致（strix 移植代码按 strix 字段名访问，如
``settings.llm.api_base``、``settings.context.summary_max_tokens``），
同时保留 Ben 的环境变量别名（DEEPSEEK_*、BEN_*）与扁平执行器/授权白名单配置。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# 项目根目录（本文件位于 Ben/config/，故上溯两级）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 哨兵：未设置 BEN_ENV_FILE 时沿用类上声明的默认 .env
_UNSET: object = object()


def resolve_env_file() -> Any:
    """解析 ``BEN_ENV_FILE`` 覆盖。

    - 未设置 -> ``_UNSET``（沿用 ``PROJECT_ROOT/.env``）；
    - 空字符串 -> ``None``（关闭 .env 读取，仅用环境变量）；
    - 其他值 -> 该路径。

    测试用它隔离开发者机器上的真实 ``.env``；部署时可指向另一份配置。
    """
    if "BEN_ENV_FILE" not in os.environ:
        return _UNSET
    raw = os.environ["BEN_ENV_FILE"].strip()
    return raw or None


class _BenSettings(BaseSettings):
    """所有 settings 类的公共基类：让 ``BEN_ENV_FILE`` 对子模型也生效。

    pydantic-settings 的 ``_env_file`` 构造参数只作用于被实例化的那一层；
    嵌套的 ``BaseSettings`` 子模型会各自按 ``model_config`` 重新读一遍
    ``.env``，导致顶层关掉了、子模型还在读。这里在 source 层统一替换
    dotenv 来源，保证开关对整个配置树一致生效。
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        env_file = resolve_env_file()
        if env_file is _UNSET:
            return init_settings, env_settings, dotenv_settings, file_secret_settings
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(
                settings_cls,
                env_file=env_file,
                env_file_encoding="utf-8",
            ),
            file_secret_settings,
        )

_BASE_CONFIG = SettingsConfigDict(
    case_sensitive=False,
    populate_by_name=True,
    extra="ignore",
    # Ben 工作流以项目根 .env 为配置来源
    env_file=str(PROJECT_ROOT / ".env"),
    env_file_encoding="utf-8",
)

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
ApiType = Literal["responses", "chat_completions"]

# SDK 层面的默认轮数上限（Ben 的 llm.max_turns 是 RunConfig 用的较小值）
DEFAULT_MAX_TURNS = 500


def _lowercase(value: object) -> object:
    """枚举型 env 值大小写不敏感（``None`` 即 ``none``）。"""
    return value.strip().lower() if isinstance(value, str) else value


class LlmSettings(_BenSettings):
    """大模型配置（厂商中立：LLM_* 为主，DEEPSEEK_*/BEN_* 为历史兼容别名）。

    两种用法：
    1. OpenAI 兼容端点：``model`` 写裸模型名（如 ``kimi-k2.6``），
       同时配 ``api_base`` + ``api_key`` 指向该端点；
    2. LiteLLM 多厂商路由：``model`` 写 ``厂商/模型``（如 ``moonshot/kimi-k2.6``、
       ``deepseek/deepseek-chat``），``api_key`` 用通用 LLM_API_KEY 或厂商专属
       环境变量（如 MOONSHOT_API_KEY），``api_base`` 留空走 LiteLLM 目录，
       或用厂商专属 *_API_BASE（如 MOONSHOT_API_BASE）覆盖目录端点。
    """

    model_config = _BASE_CONFIG

    model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_MODEL", "BEN_LLM", "DEEPSEEK_MODEL"),
    )
    api_type: ApiType | None = Field(
        default=None,
        validation_alias=AliasChoices("BEN_API_TYPE", "BEN_FORCE_API"),
        description="强制 'responses' 或 'chat_completions' API 路径",
    )
    api_key: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"),
        repr=False,
    )
    # Ben 字段名是 api_base；旧代码的 base_url 以 property 形式兼容（见下）
    # default=None：LiteLLM 目录路由（厂商/模型）时不能有默认 base，否则会污染路由
    api_base: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LLM_API_BASE",
            "OPENAI_API_BASE",
            "OPENAI_BASE_URL",
            "LITELLM_BASE_URL",
            "OLLAMA_API_BASE",
            "DEEPSEEK_BASE_URL",
        ),
    )
    extra_headers: dict[str, str] | None = Field(
        default=None, alias="LLM_EXTRA_HEADERS", repr=False
    )
    extra_body: dict[str, Any] | None = Field(
        default=None, alias="LLM_EXTRA_BODY", repr=False
    )
    reasoning_effort: ReasoningEffort = Field(default="high", alias="BEN_REASONING_EFFORT")
    input_cost_per_mtok: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, alias="BEN_LLM_INPUT_COST_PER_MTOK"
    )
    output_cost_per_mtok: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, alias="BEN_LLM_OUTPUT_COST_PER_MTOK"
    )
    cached_input_cost_per_mtok: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, alias="BEN_LLM_CACHED_INPUT_COST_PER_MTOK"
    )
    force_required_tool_choice: bool = Field(
        default=False, alias="BEN_FORCE_REQUIRED_TOOL_CHOICE"
    )
    parallel_tool_calls: bool = Field(default=False, alias="BEN_PARALLEL_TOOL_CALLS")
    prompt_cache: bool = Field(default=True, alias="BEN_PROMPT_CACHE")
    # 提供商按固定 token 块缓存 prompt，全缓存命中可能读回少一块。
    # 128 覆盖最大常见块（OpenAI；DeepSeek/GLM 为 64，vLLM 默认 16）。
    cache_block_tokens: int = Field(default=128, ge=1, alias="BEN_CACHE_BLOCK_TOKENS")
    openrouter_sticky_sessions: bool = Field(
        default=False, alias="BEN_OPENROUTER_STICKY_SESSIONS"
    )
    disable_streaming: bool = Field(default=False, alias="LLM_DISABLE_STREAMING")
    timeout: float = Field(
        default=60.0,
        validation_alias=AliasChoices("LLM_TIMEOUT", "DEEPSEEK_TIMEOUT"),
    )
    preflight_timeout: int = Field(default=30, ge=1, alias="LLM_PREFLIGHT_TIMEOUT")
    stream_idle_timeout: int = Field(default=300, ge=0, alias="LLM_STREAM_IDLE_TIMEOUT")
    stream_first_event_timeout: int = Field(
        default=120, ge=0, alias="LLM_STREAM_FIRST_EVENT_TIMEOUT"
    )
    stream_total_timeout: int = Field(default=600, ge=0, alias="LLM_STREAM_TOTAL_TIMEOUT")
    max_tool_calls_per_turn: int = Field(
        default=32, ge=0, alias="LLM_MAX_TOOL_CALLS_PER_TURN"
    )
    # Ben 特有：RunConfig 的轮数上限
    max_turns: int = Field(
        default=12,
        validation_alias=AliasChoices("MAX_AGENT_TURNS", "MAX_TURNS"),
    )

    @field_validator("api_type", "reasoning_effort", mode="before")
    @classmethod
    def _normalize_case(cls, value: object) -> object:
        return _lowercase(value)

    @field_validator("api_key", "api_base", "model", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @property
    def base_url(self) -> str | None:
        """Ben 兼容属性：``api_base`` 的别名。"""
        return self.api_base

    def vendor_key_sufficient(self) -> bool:
        """带前缀模型走 LiteLLM 目录路由时，厂商专属环境变量可替代通用 key。

        例如 ``moonshot/kimi-k2.6`` + ``MOONSHOT_API_KEY``：通用 ``api_key``
        为空也是合法配置。裸模型名（OpenAI 兼容端点模式）必须配通用 key。
        """
        model = (self.model or "").strip().lower()
        if "/" not in model:
            return False
        for prefix in ("litellm/", "any-llm/"):
            if model.startswith(prefix):
                model = model[len(prefix) :]
                break
        try:
            import litellm

            report = litellm.validate_environment(model=model)
        except Exception:  # noqa: BLE001 - 目录不认识的模型交给运行时判错
            return False
        return not report.get("missing_keys")


class DedupeSettings(_BenSettings):
    """报告去重用的独立 LLM 配置。"""

    model_config = _BASE_CONFIG

    model: str | None = Field(default=None, alias="BEN_DEDUPE_MODEL")
    reasoning_effort: ReasoningEffort | None = Field(
        default=None, alias="BEN_DEDUPE_REASONING_EFFORT"
    )
    api_key: str | None = Field(default=None, alias="DEDUPE_LLM_API_KEY", repr=False)
    api_base: str | None = Field(default=None, alias="DEDUPE_LLM_API_BASE")
    extra_headers: dict[str, str] | None = Field(
        default=None, alias="DEDUPE_LLM_EXTRA_HEADERS", repr=False
    )
    extra_body: dict[str, Any] | None = Field(
        default=None, alias="DEDUPE_LLM_EXTRA_BODY", repr=False
    )

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_case(cls, value: object) -> object:
        return _lowercase(value)


class ContextSettings(_BenSettings):
    """上下文窗口管理：工具输出上限 + 历史压缩。"""

    model_config = _BASE_CONFIG

    auto_compact: bool = Field(default=True, alias="BEN_CONTEXT_AUTO_COMPACT")
    compact_buffer_tokens: int = Field(
        default=20_000, gt=0, alias="BEN_CONTEXT_BUFFER_TOKENS"
    )
    keep_tokens: int = Field(default=8_000, gt=0, alias="BEN_CONTEXT_KEEP_TOKENS")
    fallback_context_tokens: int = Field(
        default=200_000, gt=0, alias="BEN_CONTEXT_FALLBACK_TOKENS"
    )
    summary_max_tokens: int = Field(
        default=4_096, gt=0, alias="BEN_CONTEXT_SUMMARY_TOKENS"
    )
    tool_output_max_tokens: int = Field(
        default=8_000, gt=0, alias="BEN_TOOL_OUTPUT_MAX_TOKENS"
    )
    tool_output_max_lines: int = Field(
        default=2_000,
        gt=0,
        validation_alias=AliasChoices("TOOL_OUTPUT_MAX_LINES", "BEN_TOOL_OUTPUT_MAX_LINES"),
    )
    # 下限高于截断通知本身的大小，保证预览一定放得下
    tool_output_max_bytes: int = Field(
        default=50 * 1024,
        ge=1024,
        validation_alias=AliasChoices("TOOL_OUTPUT_MAX_BYTES", "BEN_TOOL_OUTPUT_MAX_BYTES"),
    )


class RuntimeSettings(_BenSettings):
    """运行时后端配置（Ben 另有扁平 executor_* 字段）。"""

    model_config = _BASE_CONFIG

    image: str = Field(
        default="ghcr.io/usestrix/strix-sandbox:1.3.1", alias="BEN_IMAGE"
    )
    backend: str = Field(default="docker", alias="BEN_RUNTIME_BACKEND")
    # 每个 agent 上下文中保留的截图/图片工具输出上限（0 = 不保留）
    max_context_images: int = Field(default=3, ge=0, alias="BEN_MAX_CONTEXT_IMAGES")


class TelemetrySettings(_BenSettings):
    """遥测开关（Ben 的 telemetry 包已无害化为 no-op，字段保留兼容）。"""

    model_config = _BASE_CONFIG

    enabled: bool = Field(default=False, alias="BEN_TELEMETRY")
    run_record_timings: bool = Field(default=False, alias="BEN_RUN_RECORD_TIMINGS")


WebSearchProvider = Literal["auto", "perplexity", "exa"]
ExaSearchType = Literal["auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"]


class IntegrationSettings(_BenSettings):
    """第三方集成密钥。"""

    model_config = _BASE_CONFIG

    perplexity_api_key: str | None = Field(default=None, alias="PERPLEXITY_API_KEY", repr=False)
    exa_api_key: str | None = Field(default=None, alias="EXA_API_KEY", repr=False)
    web_search_provider: WebSearchProvider = Field(
        default="auto", alias="BEN_WEB_SEARCH_PROVIDER"
    )
    exa_search_type: ExaSearchType = Field(default="auto", alias="BEN_EXA_SEARCH_TYPE")
    exa_num_results: int = Field(default=5, ge=1, le=100, alias="BEN_EXA_NUM_RESULTS")
    postman_api_key: str | None = Field(default=None, alias="POSTMAN_API_KEY", repr=False)


class ViewerSettings(_BenSettings):
    model_config = _BASE_CONFIG

    app_url: str = Field(default="https://app.strix.ai", alias="BEN_APP_URL")


class Settings(_BenSettings):
    """运行配置（子模型 + Ben 扁平配置）。"""

    model_config = _BASE_CONFIG

    # --- 子模型 ---
    llm: LlmSettings = Field(default_factory=LlmSettings)
    dedupe: DedupeSettings = Field(default_factory=DedupeSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    integrations: IntegrationSettings = Field(default_factory=IntegrationSettings)
    viewer: ViewerSettings = Field(default_factory=ViewerSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)

    # --- 兼容属性：旧代码对 settings.deepseek_* 等的直接访问 ---
    @property
    def deepseek_api_key(self) -> str:
        return self.llm.api_key

    @property
    def deepseek_model(self) -> str | None:
        return self.llm.model

    @property
    def deepseek_base_url(self) -> str | None:
        return self.llm.api_base

    @property
    def llm_timeout(self) -> float:
        return self.llm.timeout

    @property
    def max_agent_turns(self) -> int:
        return self.llm.max_turns

    # --- 授权目标（白名单，Ben 特有）---
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

    # --- Docker 执行器（Ben 特有扁平字段）---
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

    # --- 工具输出上限（转发到 context 子模型，保持 Ben 扁平访问兼容）---
    @property
    def tool_output_max_lines(self) -> int:
        return self.context.tool_output_max_lines

    @property
    def tool_output_max_bytes(self) -> int:
        return self.context.tool_output_max_bytes

    # --- 输出 ---
    reports_dir: Path = Field(default=PROJECT_ROOT / "reports")
    prompts_dir: Path = Field(
        default=Path(__file__).resolve().parents[1] / "agents" / "prompts",
    )

    def validate(self) -> list[str]:
        """返回配置错误列表（空列表表示通过）。

        API Key 两种合法形态：
        1. 通用 ``LLM_API_KEY``（任意模式可用，会自动镜像到厂商专属变量）；
        2. 带前缀模型 + 厂商专属变量（如 ``moonshot/kimi-k2.6`` + ``MOONSHOT_API_KEY``）。
        """
        problems: list[str] = []
        if not self.llm.model:
            problems.append(
                "缺少模型配置：.env 中设置 LLM_MODEL"
                "（OpenAI 兼容端点写裸模型名；多厂商路由写 厂商/模型，如 moonshot/kimi-k2.6）"
            )
        from Ben.config.codex import subscription_model

        if not subscription_model(self.llm.model) and not self.llm.api_key and not self.llm.vendor_key_sufficient():
            problems.append(
                "缺少 API Key：.env 中设置 LLM_API_KEY，"
                "或使用带前缀模型并配置厂商专属变量（如 MOONSHOT_API_KEY）"
            )
        return problems


def load_settings(*, require_api_key: bool = True) -> Settings:
    """从 .env / 环境变量加载配置并校验（Ben 原版入口）。

    移植代码应使用 :func:`Ben.config.loader.load_settings`（memoized、
    支持 JSON 配置文件覆盖）；本函数保留给 Ben 的 interface 启动路径。
    """
    settings = Settings()
    problems = settings.validate()
    if require_api_key and problems:
        raise ValueError("；".join(problems))
    return settings
