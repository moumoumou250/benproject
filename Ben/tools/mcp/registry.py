"""一次扫描可触达的 MCP 连接的逐运行 registry（对齐 strix 的 mcp/registry.py）。

取代逐工具注册。旧模型把每个已连接 MCP 服务器的每个工具都注册成独立的
agent 工具，一次运行挂几条连接就把几十个 provider 工具 schema 压上 root
agent 的第一个 LLM 请求。现在改为：运行把活跃连接保存在这里，以用户给每条
连接起的名字为键，每个 agent 通过通用派发工具触达它们：``list_mcps`` 发现
可用连接，``describe_mcp`` 按需了解一条连接的工具 schema，``call_mcp`` 运
行它的一个工具。

每次运行构建一个 :class:`McpRegistry`（在 :mod:`Ben.core.runner`），存放在
运行上下文的 :data:`MCP_REGISTRY_CONTEXT_KEY` 键下，由 root agent 与每个子
agent 共享（子 agent 上下文是父上下文的拷贝，携带同一个 registry 对象）。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from Ben.tools.mcp.session import McpConnectionUnavailableError, SupervisedMcpSession


if TYPE_CHECKING:
    from collections.abc import Callable

    from agents.mcp import MCPServer

    from Ben.tools.mcp.client import ResultTransform
    from Ben.tools.mcp.config import McpConnectionConfig


# 运行上下文中存放逐运行 registry 的键，派发工具也从这里读回。放在这里，让工
# 具与 runner 认同同一个名字。
MCP_REGISTRY_CONTEXT_KEY = "mcp_registry"


# 界面层归属到具体某条 MCP 连接的连接级派发工具。``call_mcp`` 在一条连接上运
# 行一个工具；``describe_mcp`` 列出一条连接的工具 schema。（``list_mcps``
# 刻意不在内：它不指名单条连接，渲染为普通工具调用。）放在这里（而非界面
# 层），让引擎与界面按同一组名字识别连接级派发调用。
CALL_MCP_TOOL = "call_mcp"
DESCRIBE_MCP_TOOL = "describe_mcp"
SEARCH_MCP_TOOLS_TOOL = "search_mcp_tools"
GET_MCP_TOOL_SCHEMA_TOOL = "get_mcp_tool_schema"
MCP_DISPATCH_TOOLS = frozenset(
    {
        CALL_MCP_TOOL,
        DESCRIBE_MCP_TOOL,
        SEARCH_MCP_TOOLS_TOOL,
        GET_MCP_TOOL_SCHEMA_TOOL,
    }
)

McpConnectionState = Literal[
    "configured",
    "connecting",
    "connected",
    "catalog_loading",
    "catalog_ready",
    "unavailable",
]
_RETRY_DELAY_SECONDS = 5.0


@dataclasses.dataclass
class McpConnectionEntry:
    """一次扫描可触达的一条已配置 MCP 连接，以 ``name`` 为键。

    注册是惰性的。第一次预热、搜索、schema 查询或调用创建一个共享的连接任
    务；第一次目录操作创建一个共享的列目录任务。因此 root 与子 agent 即使并
    发请求冷连接，也复用同一个会话与同一份目录。
    """

    name: str
    connection_config: McpConnectionConfig | None = dataclasses.field(
        default=None,
        repr=False,
    )
    session: SupervisedMcpSession | None = dataclasses.field(default=None, repr=False)
    purpose: str | None = None
    tool_count: int = 0
    result_transform: ResultTransform | None = None
    provider: str | None = None
    state: McpConnectionState = "configured"
    _catalog: list[Any] | None = dataclasses.field(default=None, repr=False)
    _connect_task: asyncio.Task[SupervisedMcpSession] | None = dataclasses.field(
        default=None,
        repr=False,
    )
    _catalog_task: asyncio.Task[list[Any]] | None = dataclasses.field(
        default=None,
        repr=False,
    )
    _retry_after: float = dataclasses.field(default=0.0, repr=False)
    _status_sink: Callable[[], None] | None = dataclasses.field(default=None, repr=False)

    @property
    def server(self) -> MCPServer | None:
        """会话背后当前的活跃服务器（重连时替换）。

        保留它是为了让读 ``entry.server`` 的既有调用方继续工作；新代码应通过
        ``entry.session`` 调用，让重连与收容生效。
        """
        return self.session.server if self.session is not None else None

    @property
    def config(self) -> McpConnectionConfig | None:
        """重连配置。携带 bearer 令牌；绝不记日志。"""
        if self.connection_config is not None:
            return self.connection_config
        return self.session.config if self.session is not None else None

    @property
    def active_tools(self) -> frozenset[str]:
        config = self.config
        return frozenset(config.active_tools if config is not None else ())

    def set_status_sink(self, sink: Callable[[], None] | None) -> None:
        self._status_sink = sink
        if self.session is not None:
            self.session.set_on_dead(self._on_dead)

    def _set_state(self, state: McpConnectionState) -> None:
        if self.state == state:
            return
        self.state = state
        if self._status_sink is not None:
            self._status_sink()

    def _on_dead(self) -> None:
        self.session = None
        self._catalog = None
        self._catalog_task = None
        self._retry_after = time.monotonic() + _RETRY_DELAY_SECONDS
        self._set_state("unavailable")

    async def ensure_connected(self) -> SupervisedMcpSession:
        """返回本条目的活跃会话，需要时连接一次。"""
        if self.session is not None:
            return self.session
        if self.connection_config is None:
            raise McpConnectionUnavailableError(
                f"MCP connection {self.name!r} is unavailable and cannot reconnect."
            )
        if self._connect_task is None:
            if self.state == "unavailable" and time.monotonic() < self._retry_after:
                raise McpConnectionUnavailableError(
                    f"MCP connection {self.name!r} is temporarily unavailable."
                )
            self._connect_task = asyncio.create_task(
                self._connect(),
                name=f"mcp-connect-{self.name}",
            )
        task = self._connect_task
        try:
            return await asyncio.shield(task)
        finally:
            if self._connect_task is task and task.done():
                self._connect_task = None

    async def _connect(self) -> SupervisedMcpSession:
        config = self.connection_config
        if config is None:
            raise McpConnectionUnavailableError(
                f"MCP connection {self.name!r} has no connection configuration."
            )
        self._set_state("connecting")
        session = SupervisedMcpSession(config)
        try:
            started = await session.start()
        except BaseException:
            with contextlib.suppress(BaseException):
                await session.aclose()
            self._retry_after = time.monotonic() + _RETRY_DELAY_SECONDS
            self._set_state("unavailable")
            raise
        if not started:
            await session.aclose()
            self._retry_after = time.monotonic() + _RETRY_DELAY_SECONDS
            self._set_state("unavailable")
            raise McpConnectionUnavailableError(f"MCP connection {self.name!r} could not connect.")
        self.session = session
        session.set_on_dead(self._on_dead)
        self._retry_after = 0.0
        self._set_state("connected")
        return session

    async def ensure_catalog(self) -> list[Any]:
        """返回过滤后的目录，首次使用时列一次。"""
        if (
            self._catalog is not None
            and self.session is not None
            and not self.session.is_dead
            and not self.session.is_unavailable
        ):
            return self._catalog
        if self.session is not None and (self.session.is_dead or self.session.is_unavailable):
            self._catalog = None
        if self._catalog_task is None:
            self._catalog_task = asyncio.create_task(
                self._load_catalog(),
                name=f"mcp-catalog-{self.name}",
            )
        task = self._catalog_task
        try:
            return await asyncio.shield(task)
        finally:
            if self._catalog_task is task and task.done():
                self._catalog_task = None

    async def _load_catalog(self) -> list[Any]:
        session = await self.ensure_connected()
        self._set_state("catalog_loading")
        try:
            catalog = await session.list_tools()
        except BaseException:
            if session.is_dead:
                self._set_state("unavailable")
            else:
                self._set_state("connected")
            raise
        self._catalog = list(catalog)
        self.tool_count = len(self._catalog)
        self._set_state("catalog_ready")
        return self._catalog

    async def close(self) -> None:
        """取消未完成的初始化并关闭已打开的会话。"""
        tasks = [task for task in (self._catalog_task, self._connect_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        self._catalog_task = None
        self._connect_task = None
        if self.session is not None:
            with contextlib.suppress(BaseException):
                await self.session.aclose()
        self.session = None


@dataclasses.dataclass(frozen=True)
class McpConnectionSummary:
    """``list_mcps`` 返回的一条连接摘要：agent 决定是否 ``describe_mcp`` 该
    连接所需的信息，不含工具 schema。"""

    name: str
    purpose: str | None
    tool_count: int
    provider: str | None = None
    state: McpConnectionState = "configured"


@dataclasses.dataclass(frozen=True)
class McpConnectionStatus:
    """一条连接给界面层的即时状态。

    构造上即非敏感：只有连接 ``name``、``provider`` 标签、``tool_count``，
    以及它的活跃会话当前是否 ``dead``（重连重试已放弃）。config、令牌、
    url、purpose 都不在这里。``dead`` 在构建时从连接的会话上即时读取，因此
    新的 :meth:`McpRegistry.statuses` 反映当前健康度。
    """

    name: str
    provider: str | None
    tool_count: int
    dead: bool
    state: McpConnectionState


@dataclasses.dataclass(frozen=True)
class McpConnectionRequest:
    """来源无关的"把一条 MCP 连接挂到本次运行"请求。

    调用方交给引擎一份惰性 ``config``（如何到达服务器、名字、可选的认证令
    牌）加元数据，绝不交活跃会话：连接与清理由引擎拥有。``provider`` 是可
    选的来源标签（例如 ``"supabase"``；命令行路径为空）。``result_transform``
    是可选的逐连接变换，在单一派发点对每次调用的结构化结果运行。
    ``purpose`` 是 ``list_mcps`` 报告为连接描述的人类可读标签；未设置时回退
    到 ``config.notes``。
    """

    config: McpConnectionConfig
    provider: str | None = None
    result_transform: ResultTransform | None = None
    purpose: str | None = None


class McpCallInfo(NamedTuple):
    """一次 MCP 派发调用的归属：连接名、底层工具（``describe_mcp`` 为空），
    以及连接的 provider 标签（未知或未打标时为 ``None``）。"""

    connection: str
    tool: str
    provider: str | None


class McpRegistry:
    """连接名 -> 活跃 MCP 连接，逐运行构建，由运行中的每个 agent 共享。

    公开 API：构造函数、:meth:`add`、:meth:`get`、:meth:`summaries`。
    """

    def __init__(self) -> None:
        self._entries: dict[str, McpConnectionEntry] = {}
        self._status_sink: Callable[[], None] | None = None
        self._warmup_task: asyncio.Task[None] | None = None

    def register(self, request: McpConnectionRequest) -> McpConnectionEntry:
        """注册一个惰性请求，不打开网络连接。"""
        entry = McpConnectionEntry(
            name=request.config.name,
            connection_config=request.config,
            purpose=request.purpose or request.config.notes,
            result_transform=request.result_transform,
            provider=request.provider,
        )
        entry.set_status_sink(self._status_sink)
        self._entries[entry.name] = entry
        return entry

    def add(
        self,
        *,
        name: str,
        session: SupervisedMcpSession | None = None,
        server: MCPServer | None = None,
        config: McpConnectionConfig | None = None,
        purpose: str | None = None,
        tool_count: int = 0,
        result_transform: ResultTransform | None = None,
        provider: str | None = None,
    ) -> McpConnectionEntry:
        """把一条连接注册到 ``name`` 下（后写覆盖先写）。

        传 ``session`` 用于引擎已在监督的会话（挂接路径如此）。传 ``server``
        用于调用方自己持有的已连接服务器：它被收养进一个针对它内联运行调用
        的会话，且仅当同时给出 ``config`` 时才可重连。``session`` 与
        ``server`` 恰需其一。
        """
        if session is None:
            if server is None:
                raise ValueError("McpRegistry.add requires either 'session' or 'server'")
            session = SupervisedMcpSession.adopt(server, name=name, config=config)
        entry = McpConnectionEntry(
            name=name,
            connection_config=config or session.config,
            session=session,
            purpose=purpose,
            tool_count=tool_count,
            result_transform=result_transform,
            provider=provider,
            state="connected",
        )
        entry.set_status_sink(self._status_sink)
        self._entries[name] = entry
        return entry

    def get(self, name: str) -> McpConnectionEntry | None:
        """注册在 ``name`` 下的连接，或 ``None``。"""
        return self._entries.get(name)

    def names(self) -> list[str]:
        """已注册的连接名，按插入顺序。"""
        return list(self._entries)

    def summaries(self) -> list[McpConnectionSummary]:
        """每条连接一份清单摘要，按插入顺序。"""
        return [
            McpConnectionSummary(
                name=entry.name,
                purpose=entry.purpose,
                tool_count=entry.tool_count,
                provider=entry.provider,
                state=entry.state,
            )
            for entry in self._entries.values()
        ]

    def statuses(self) -> list[McpConnectionStatus]:
        """每条连接一份即时状态，按插入顺序。

        在调用时从各连接的会话读取 ``dead`` 标志，界面层每次重建都拿到当前
        健康度。非敏感：只有 name、provider、tool_count、dead。"""
        return [
            McpConnectionStatus(
                name=entry.name,
                provider=entry.provider,
                tool_count=entry.tool_count,
                dead=entry.state == "unavailable"
                or (entry.session is not None and entry.session.is_dead),
                state=entry.state,
            )
            for entry in self._entries.values()
        ]

    def clear(self) -> None:
        """丢弃所有连接（会话本身由 runner 关闭）。"""
        self._entries.clear()

    def set_status_sink(self, sink: Callable[[], None] | None) -> None:
        """在任一连接生命周期跃迁后接收回调。"""
        self._status_sink = sink
        for entry in self._entries.values():
            entry.set_status_sink(sink)

    def start_warmup(self, *, max_concurrency: int = 6) -> asyncio.Task[None]:
        """在固定并发上限下连接并列目录每个已配置条目。"""
        if self._warmup_task is not None:
            return self._warmup_task

        async def warm() -> None:
            semaphore = asyncio.Semaphore(max(1, max_concurrency))

            async def load_catalog(entry: McpConnectionEntry) -> None:
                async with semaphore:
                    with contextlib.suppress(Exception):
                        await entry.ensure_catalog()

            await asyncio.gather(*(load_catalog(entry) for entry in self._entries.values()))

        self._warmup_task = asyncio.create_task(warm(), name="mcp-warmup")
        return self._warmup_task

    async def close(self) -> None:
        """停止预热，并仅关闭本 registry 打开的会话。"""
        if self._warmup_task is not None:
            self._warmup_task.cancel()
            with contextlib.suppress(BaseException):
                await self._warmup_task
            self._warmup_task = None
        await asyncio.gather(
            *(entry.close() for entry in self._entries.values()),
            return_exceptions=True,
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)


def resolve_mcp_call(
    tool_name: str,
    args: dict[str, Any],
    registry: McpRegistry | None = None,
) -> McpCallInfo | None:
    """把一次工具调用解析到它发向的 MCP 连接/工具/provider。

    界面层与追踪器都经这同一个解析器读派发调用，因此一次调用在各处的归属
    一致。agent 发起的每次 MCP 调用都走 ``call_mcp`` 或 ``describe_mcp``，
    连接名（以及 ``call_mcp`` 的服务器工具名）携带在调用的 ``args`` 里而非
    工具名里，因此从这里读取。

    当 ``tool_name`` 不是派发工具之一、调用未携带连接名、或提供了
    ``registry`` 而其下无该连接时返回 ``None``。``tool`` 对 ``call_mcp``
    是底层工具，对 ``describe_mcp``（它检视连接本身）为空。``provider``
    来自 registry 条目；未提供 ``registry`` 或连接无 provider 标签时为
    ``None``。
    """
    if tool_name not in MCP_DISPATCH_TOOLS:
        return None
    connection = args.get("connection")
    if not isinstance(connection, str) or not connection:
        return None
    provider: str | None = None
    if registry is not None:
        entry = registry.get(connection)
        if entry is None:
            return None
        provider = entry.provider
    raw_tool = args.get("tool") if tool_name in {CALL_MCP_TOOL, GET_MCP_TOOL_SCHEMA_TOOL} else ""
    tool = raw_tool if isinstance(raw_tool, str) else ""
    return McpCallInfo(connection=connection, tool=tool, provider=provider)
