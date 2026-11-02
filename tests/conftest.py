"""共享测试夹具（适配自 strix-main/tests/conftest.py）。"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession

# LiteLLM 默认会去 raw.githubusercontent.com 拉取价格表，本机网络不通时每次
# 导入要等 3 次读超时（整套测试因此多花好几分钟且不稳定）。测试只关心价格
# 解析逻辑，用随包发布的本地价格表即可。必须在 import litellm 之前设置。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# Ben 包未安装到 site-packages，直接把仓库根目录插进 sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class StubSandboxSession(BaseSandboxSession):
    """最小沙箱会话桩：满足 capability.bind() 的接口要求，不做任何真实 IO。

    ``build_agent`` 需要 ``sandbox_session`` 才能让 Shell/Filesystem
    capability 生成 exec_command / apply_patch 等工具。单元测试只关心
    工具是否被正确挂载与包装，不需要真容器，因此用这个桩替代。
    """

    def __init__(self) -> None:
        super().__init__()
        self.workspace_root = Path("/workspace")
        self.commands: list[tuple[str, ...]] = []

    @property
    def running(self) -> bool:
        return True

    async def _exec_internal(self, *command: Any, timeout: float | None = None) -> Any:
        self.commands.append(tuple(str(c) for c in command))
        raise NotImplementedError("StubSandboxSession 不支持真实执行")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        return None

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def read(self, path: Path, *, user: Any = None) -> io.IOBase:
        return io.BytesIO()

    async def write(self, path: Path, data: io.IOBase, *, user: Any = None) -> None:
        return None

    async def __aenter__(self) -> StubSandboxSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


_STUB_SESSION = StubSandboxSession()

# .env 会被同步成这些环境变量；测试里统一清掉以保证配置来源可控
_DOTENV_ALIASES = (
    "LLM_MODEL",
    "BEN_LLM",
    "DEEPSEEK_MODEL",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_API_BASE",
    "OPENAI_BASE_URL",
    "DEEPSEEK_BASE_URL",
    "ALLOWED_TARGETS",
    "MAX_AGENT_TURNS",
    "COMMAND_TIMEOUT",
    "EXECUTOR_IMAGE",
    "EXECUTOR_NETWORK",
    "EXECUTOR_CONTAINER",
    "LLM_TIMEOUT",
)


def build_agent(**kwargs: Any) -> Any:
    """测试用 agent 构造器：自动注入 stub session，其余参数原样透传。"""
    from Ben.agents import factory

    kwargs.setdefault("sandbox_session", _STUB_SESSION)
    return factory.build_agent(**kwargs)


@pytest.fixture
def sandbox_session() -> StubSandboxSession:
    """每个用例一个独立的沙箱桩，便于断言执行记录。"""
    return StubSandboxSession()


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离开发者机器上的真实 .env。

    生产默认读 ``PROJECT_ROOT/.env``；测试必须自己决定配置来源，否则
    真实的 ``LLM_MODEL=kimi-k2.6`` 会覆盖用例期望值。这里既关掉 .env
    读取，也清掉可能已被其它模块写进 ``os.environ`` 的别名值
    （``Ben.interface.main`` 导入时会把 .env 同步成环境变量）。
    """
    monkeypatch.setenv("BEN_ENV_FILE", "")
    for key in _DOTENV_ALIASES:
        monkeypatch.delenv(key, raising=False)
    from Ben.config import loader

    monkeypatch.setattr(loader, "_cached", None)


@pytest.fixture(autouse=True)
def _isolate_mcp_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """隔离开发者机器上真实的 MCP 配置。

    把加载器指向一个不存在的路径，使其解析为"无连接"，
    并清掉每次运行的选择环境变量。测试加载器自身的用例
    在此之后自行设置 ``BEN_MCP_CONFIG``，因此会覆盖这里的值。
    """
    missing = tmp_path_factory.mktemp("mcp-isolation") / "no-servers.json"
    monkeypatch.setenv("BEN_MCP_CONFIG", str(missing))
    monkeypatch.delenv("BEN_MCP_ONLY", raising=False)
    monkeypatch.delenv("BEN_MCP_EXCLUDE", raising=False)


@pytest.fixture(autouse=True)
def _plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 Rich 输出在每台开发机上保持一致。

    许多 CLI 测试会把 ``isatty()`` 强制为 ``True`` 并对纯文本断言。
    dumb 终端渲染同样的文本但不带任何样式。
    """
    monkeypatch.setenv("TERM", "dumb")
    for name in ("COLORTERM", "FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE"):
        monkeypatch.delenv(name, raising=False)
