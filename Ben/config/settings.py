"""配置读取与校验。

所有环境相关的值（API Key、模型名、API 地址、授权目标、执行器参数）都从
环境变量 / 项目根的 .env 文件读取，不在源码中硬编码密钥。

运行前先复制 .env.example 为 .env 并填写真实 DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（本文件位于 Ben/config/，故上溯两级）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """逗号分隔的环境变量 -> 字符串列表（去空白、去空项、统一小写便于匹配）。"""
    raw = _env(name)
    if not raw:
        return tuple(default)
    return tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    """运行配置。用 :func:`load_settings` 构造并校验。"""

    # --- 大模型 ---
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    llm_timeout: float
    max_agent_turns: int

    # --- 授权目标（白名单）---
    # 只有命中白名单的目标才允许工具访问；容器默认不允许访问任意公网。
    allowed_targets: tuple[str, ...]

    # --- Docker 执行器 ---
    executor_image: str
    executor_network: str
    executor_container: str
    command_timeout: float

    # --- 输出 ---
    reports_dir: Path
    prompts_dir: Path

    extra: dict[str, str] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """返回配置错误列表（空列表表示通过）。"""
        problems: list[str] = []
        if not self.deepseek_api_key:
            problems.append("缺少 DEEPSEEK_API_KEY：请复制 .env.example 为 .env 并填写 API Key")
        if not self.deepseek_model:
            problems.append("缺少 DEEPSEEK_MODEL")
        if not self.deepseek_base_url:
            problems.append("缺少 DEEPSEEK_BASE_URL")
        if not self.allowed_targets:
            problems.append("缺少 ALLOWED_TARGETS：至少配置一个已授权目标")
        if self.max_agent_turns < 1:
            problems.append("MAX_AGENT_TURNS 必须 >= 1")
        return problems


def load_settings(*, require_api_key: bool = True) -> Settings:
    """加载 .env 与环境变量，构造并校验配置。

    require_api_key=False 时不强制要求 API Key（用于仅验证 Docker 的场景）。
    """
    # 加载项目根 .env（存在才加载），不覆盖已存在的真实环境变量。
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    settings = Settings(
        deepseek_api_key=_env("DEEPSEEK_API_KEY"),
        deepseek_model=_env("DEEPSEEK_MODEL", "deepseek-chat"),
        deepseek_base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        llm_timeout=float(_env("LLM_TIMEOUT", "60") or 60),
        max_agent_turns=int(_env("MAX_AGENT_TURNS", "12") or 12),
        # 无 .env 时默认仅允许本地授权靶场（可在 .env 覆盖）；API Key 仍必须显式配置
        # 此处设置允许审计的URL
        allowed_targets=_env_list("ALLOWED_TARGETS", ("http://juice-shop:3000",)),
        executor_image=_env(
            "EXECUTOR_IMAGE", "ghcr.io/usestrix/strix-sandbox:1.3.0"
        ),
        executor_network=_env("EXECUTOR_NETWORK", "benptnet"),
        executor_container=_env("EXECUTOR_CONTAINER", "benpt-executor"),
        command_timeout=float(_env("COMMAND_TIMEOUT", "30") or 30),
        reports_dir=PROJECT_ROOT / "reports",
        # Prompt 随 agents 包走，和 strix/agents/prompts 的放置方式一致。
        # __file__是Ben/config/settings.py的位置
        #  `.parents[0]` →`Ben/config` `.parents[1]` →`Ben`
        prompts_dir=Path(__file__).resolve().parents[1] / "agents" / "prompts",
    )

    problems = settings.validate()
    if not require_api_key:
        problems = [p for p in problems if "DEEPSEEK_API_KEY" not in p]
    if problems:
        joined = "\n  - ".join(problems)
        raise SystemExit(f"配置校验失败：\n  - {joined}")
    return settings

"""
# 当前配置
Settings(
    deepseek_api_key='sk-VKf...9CXU',
    deepseek_model='deepseek-v4.1-flash',
    deepseek_base_url='https://api.freeshare.tech:8443/v1',
    llm_timeout=60.0,
    max_agent_turns=12,
    allowed_targets=('http://juice-shop:3000',),

    # 执行strix-sandbox容器，网络名为benptnet，容器名称benpt-executor
    executor_image='ghcr.io/usestrix/strix-sandbox:1.3.0',
    executor_network='benptnet',
    executor_container='benpt-executor',
    command_timeout=30.0,
    reports_dir=WindowsPath('C:/Users/19219/WorkBuddy/BenProject/reports'),
    prompts_dir=WindowsPath('C:/Users/19219/WorkBuddy/BenProject/Ben/agents/prompts'),
    extra={},
)
"""