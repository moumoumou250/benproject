"""连接 MCP 服务器，让一次运行能按需触达它们的工具（对齐 strix 的 mcp/client.py）。

给定每个服务器一个 :class:`McpConnectionConfig`，:func:`connect_mcp_servers`
逐个连接、统计其提供的工具数（遵循连接的白名单），并返回活跃会话。这里不
向 agent 注册任何工具：在通用派发模型下，运行把这些会话放进每次运行的
:class:`~Ben.tools.mcp.registry.McpRegistry`，agent 通过派发工具
（``describe_mcp`` / ``call_mcp``）触达它们，派发工具再调这里的
:func:`dispatch_mcp_call` 运行一个工具并序列化结果。

连不上的服务器记日志并跳过，单个坏连接永远不会让运行失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from agents.mcp import (
    MCPServer,
    MCPServerStdio,
    MCPServerStdioParams,
    MCPServerStreamableHttp,
    MCPServerStreamableHttpParams,
    create_static_tool_filter,
)
from mcp.client.stdio import stdio_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import TextContent

from Ben.tools.mcp.failures import HttpStatusRecorder
from Ben.tools.mcp.session import McpConnectionUnavailableError, SupervisedMcpSession


if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from Ben.tools.mcp.config import McpConnectionConfig
    from Ben.tools.mcp.registry import McpConnectionRequest, McpRegistry

    # 在一次工具调用的结构化结果到达 agent 前运行。调用形式为
    # ``result_transform(label, structured_result)``，其返回值成为工具输出。
    # ``label`` 是模型侧的 ``<connection>_<tool>`` 名称，使按名称键控的
    # transform 仍像在逐工具注册时代那样解析；``structured_result`` 是解析成
    # dict 的 ``CallToolResult``（不是序列化字符串），因此 transform 可以投影
    # 或丢弃个别字段。
    ResultTransform = Callable[[str, Any], Any]


logger = logging.getLogger(__name__)


class ConnectedMcpServer(NamedTuple):
    """一条成功连上的 MCP 连接及其提供的工具数。

    ``session`` 是把活跃连接拥有在独立任务上的
    :class:`~Ben.tools.mcp.session.SupervisedMcpSession`，调用方在运行结束时
    清理它（``await session.aclose()``）并把它交给本次运行的
    :class:`~Ben.tools.mcp.registry.McpRegistry`；``name`` 与 ``tool_count``
    让调用方向用户展示启动摘要并填充 prompt 清单；``notes`` 携带连接可选的
    自由文本描述，供调用方在清单中作为该连接的用途展示。
    """

    session: SupervisedMcpSession
    name: str
    tool_count: int
    notes: str | None = None


class BuiltMcpServer(NamedTuple):
    """一个构建好的 SDK 服务器及其可选的 HTTP 失败记录器。"""

    server: MCPServer
    recorder: HttpStatusRecorder | None


def _auth_headers(config: McpConnectionConfig) -> dict[str, str]:
    """从连接的 Bearer 令牌构造请求头。"""
    if not config.token:
        return {}
    return {"Authorization": f"Bearer {config.token}"}


@contextlib.asynccontextmanager
async def _quiet_stdio_streams(params: Any) -> Any:
    """运行 stdio MCP 服务器时把其 stderr 丢进虚空。

    stdio MCP 服务器启动时会往 stderr 聊天（比如 filesystem 服务器会打印
    ``Allowed directories: [ ... ]``）。mcp 库默认把该 stderr 转发到父进程的
    ``sys.stderr``，即终端，banner 会污染显示。把 ``errlog`` 指向
    ``os.devnull`` 可丢弃这些杂讯。连接失败不受影响：它们仍从 ``connect``
    抛出并由 :func:`connect_mcp_servers` 记录。
    """
    with Path(os.devnull).open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as streams:
            yield streams


class _QuietMCPServerStdio(MCPServerStdio):
    """子进程 stderr 不进终端的 ``MCPServerStdio``。

    SDK 的 ``create_streams`` 调 ``stdio_client(self.params)`` 时不传
    ``errlog``，子进程 stderr 默认落到 ``sys.stderr``，会把服务器 banner 画到
    运行中的终端上。重写它即可重定向该流；stdio 传输的其余部分完全不变。
    """

    def create_streams(self) -> Any:
        return _quiet_stdio_streams(self.params)


def _build_server(config: McpConnectionConfig) -> BuiltMcpServer:
    """为一条连接构建（但不连接）SDK 服务器。

    返回的元组携带服务器；对 HTTP 连接还携带一个记录器，为持有它的会话
    留存脱敏后的响应元数据。

    当 ``allowed_tools`` 是列表时，静态过滤器使服务器连列工具都不会列出白名
    单之外的项，因此它是 ``describe_mcp`` 与 ``call_mcp`` 可见范围的权威闸
    门。为 ``None`` 时不加过滤器，列出的每个工具都可达。
    """
    tool_filter = (
        create_static_tool_filter(allowed_tool_names=config.allowed_tools)
        if config.allowed_tools is not None
        else None
    )

    if config.transport == "stdio":
        stdio_params: MCPServerStdioParams = {
            "command": cast("str", config.command),
            "args": config.args,
            "env": config.env,
        }
        return BuiltMcpServer(
            _QuietMCPServerStdio(
                params=stdio_params,
                name=config.name,
                tool_filter=tool_filter,
                cache_tools_list=True,
            ),
            None,
        )

    recorder = HttpStatusRecorder()

    def httpx_client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        client.event_hooks.setdefault("response", []).append(recorder)
        return client

    http_params: MCPServerStreamableHttpParams = {
        "url": cast("str", config.url),
        "headers": _auth_headers(config),
        "timeout": config.http_timeout_seconds,
        "sse_read_timeout": config.sse_read_timeout_seconds,
        "httpx_client_factory": httpx_client_factory,
    }
    return BuiltMcpServer(
        MCPServerStreamableHttp(
            params=http_params,
            name=config.name,
            tool_filter=tool_filter,
            cache_tools_list=True,
            client_session_timeout_seconds=config.session_timeout_seconds,
        ),
        recorder,
    )


def _without_images(result: Any) -> Any:
    content = [
        TextContent(
            type="text", text=f"[{item.mimeType} image omitted: this model cannot view images]"
        )
        if item.type == "image"
        else item
        for item in result.content
    ]
    return result.model_copy(update={"content": content})


def _mcp_result_to_tool_output(server: MCPServer, result: Any) -> Any:
    """把 ``CallToolResult`` 序列化成工具输出，与 agents SDK 一致。

    复刻 ``agents.mcp.util.MCPUtil.invoke_mcp_tool`` 的序列化（服务器要求时
    用结构化内容 JSON，否则用文本/图片内容块，单块时解包）。因为派发工具自
    己路由调用，这让 agent 看到的内容与 SDK 自建工具时逐字节一致。
    """
    if getattr(server, "use_structured_content", False) and result.structuredContent:
        return json.dumps(result.structuredContent)

    outputs: list[dict[str, Any]] = []
    for item in result.content:
        if item.type == "text":
            outputs.append({"type": "text", "text": item.text})
        elif item.type == "image":
            outputs.append(
                {"type": "image", "image_url": f"data:{item.mimeType};base64,{item.data}"}
            )
        else:
            outputs.append({"type": "text", "text": str(item.model_dump(mode="json"))})
    if len(outputs) == 1:
        return outputs[0]
    return outputs


async def dispatch_mcp_call(
    server: MCPServer,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    label: str,
    result_transform: ResultTransform | None = None,
    supports_images: bool = True,
) -> Any:
    """运行一次 MCP 工具调用并把结果转成工具输出。

    通用 ``call_mcp`` 工具共享的单一派发点。以未加前缀的工具名调
    ``server.call_tool``，然后：

    - 有 ``result_transform`` 时，把解析后的 :class:`CallToolResult` 交给它
      （``result_transform(label, structured)``）并返回其返回值；
    - 没有时，按 agents SDK 的方式序列化结果（见
      :func:`_mcp_result_to_tool_output`），当结果是 MCP 错误时经
      :func:`_errored_tool_output` 归一化，使失败能到达界面层（见该函数的
      表示方式说明，以及它为什么不破坏 agent 收到的内容）。
    """
    result = await server.call_tool(tool_name, arguments)
    if not supports_images:
        result = _without_images(result)
    if result_transform is not None:
        return result_transform(label, result.model_dump(mode="json"))
    tool_output = _mcp_result_to_tool_output(server, result)
    if getattr(result, "isError", False):
        return _errored_tool_output(tool_output)
    return tool_output


def _errored_tool_output(tool_output: Any) -> dict[str, Any]:
    """给序列化后的 MCP 错误打标记，让界面层把它渲染为失败。

    判断一次工具调用是否失败靠读取结果顶层 dict 里的 ``success`` 键
    （``success is False`` 即失败）。:func:`_mcp_result_to_tool_output` 只在
    单内容块时返回 dict；结构化内容结果返回字符串、多块结果返回列表，失败标
    记无处可放，界面会把失败的调用显示为成功。这里把每个出错结果统一归一成
    携带 ``success: False`` 的顶层 dict：

    - 单内容块（已是 dict）保留其 ``type``/``text``，并列加上
      ``success: False``。SDK 的 ToolOutput 投影只保留已知的
      ``type``/``text`` 字段、丢弃 ``success`` 后才给 agent 看，因此 agent
      收到的仍是原原本本的错误内容；
    - 列表（多块）或字符串（结构化内容）放进稳定的 ``content`` 键下，让标记
      有顶层 dict 可依附。agent 仍在 ``content`` 下收到完整错误内容。
    """
    if isinstance(tool_output, dict):
        return {**tool_output, "success": False}
    return {"success": False, "content": tool_output}


async def _count_session_tools(config: McpConnectionConfig, session: SupervisedMcpSession) -> int:
    """为启动摘要统计一个已连接会话的可达工具数。

    ``allowed_tools`` 为 ``None`` 时统计列出的全部工具；为列表时只统计其中
    的名字。该计数与 ``describe_mcp`` 将展示的一致，因为
    :func:`_build_server` 构建的静态过滤器把服务器自己的 ``list_tools`` 也
    限制在同一个白名单内。列工具与所有其他调用一样经过会话的持有任务。
    """
    allowed = config.allowed_tools
    mcp_tools = await session.list_tools()
    return sum(1 for mcp_tool in mcp_tools if allowed is None or mcp_tool.name in allowed)


async def connect_mcp_servers(
    configs: list[McpConnectionConfig],
    *,
    max_concurrency: int = 6,
) -> list[ConnectedMcpServer]:
    """在固定并发上限下并发连接各 MCP 配置。

    每条连接成为一个
    :class:`~Ben.tools.mcp.session.SupervisedMcpSession`，把 ``connect()``、
    持有中的会话与 ``cleanup()`` 拥有在同一个专属任务上，使某会话日后的后台
    失败被收容在该任务内、永远不会取消整个运行。为每个连上的会话返回一个
    :class:`ConnectedMcpServer`，携带会话（调用方在运行结束时用
    ``await session.aclose()`` 清理，并把它交给本次运行的 registry）以及连接
    名、工具数与备注。初始连接失败的连接记日志跳过而非抛出（fail-open）。

    若本协程自身在挂接中途被取消（运行正在关停），已启动的每个会话先在各自
    任务上关闭，然后再重新抛出取消，不会有孤儿会话。

    这里不向 agent 注册任何工具：调用方从这些会话构建每次运行的
    :class:`~Ben.tools.mcp.registry.McpRegistry`，agent 经
    ``describe_mcp`` / ``call_mcp`` 按需触达每个工具。
    """
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    sessions: list[SupervisedMcpSession] = []

    async def connect_one(config: McpConnectionConfig) -> ConnectedMcpServer | None:
        async with semaphore:
            session = SupervisedMcpSession(config)
            sessions.append(session)
            try:
                if not await session.start():
                    await session.aclose()
                    return None
                tool_count = await _count_session_tools(config, session)
            except McpConnectionUnavailableError:
                logger.warning("MCP 连接 %r 在首次列工具前已失效", config.name)
                await session.aclose()
                return None
            except BaseException:
                with contextlib.suppress(BaseException):
                    await session.aclose()
                raise
            logger.info("已连接 MCP 服务器 %r（%d 个工具）", config.name, tool_count)
            return ConnectedMcpServer(
                session=session,
                name=config.name,
                tool_count=tool_count,
                notes=config.notes,
            )

    try:
        results = await asyncio.gather(*(connect_one(config) for config in configs))
    except BaseException:
        await asyncio.gather(
            *(session.aclose() for session in sessions),
            return_exceptions=True,
        )
        raise
    return [result for result in results if result is not None]


async def attach_mcp_requests(
    requests: list[McpConnectionRequest],
    registry: McpRegistry,
) -> list[ConnectedMcpServer]:
    """连接调用方的 MCP 请求并填充本次运行的 registry。

    命令行与产品共用的唯一挂接-填充路径，所有连接与清理逻辑集中在同一个
    拥有者。调用方提供惰性的 :class:`McpConnectionRequest`（一份配置加一个
    provider 标签、可选的逐连接 ``result_transform``、可选的 ``purpose``），
    绝不提供活跃会话：引擎在这里连接每份配置，复用
    :func:`connect_mcp_servers`，因此 fail-open 行为（连不上的连接记日志跳
    过）与取消清理都原样保留。

    对每条连上的连接，按其配置名注册，携带工具数、``provider`` 标签、
    ``result_transform``，purpose 取 ``request.purpose``（未设置时回退到连接
    备注）。返回连上的服务器列表（由 runner 记录并在运行结束时清理）。
    """
    request_by_name = {request.config.name: request for request in requests}
    connections = await connect_mcp_servers([request.config for request in requests])
    for connection in connections:
        request = request_by_name[connection.name]
        registry.add(
            name=connection.name,
            session=connection.session,
            tool_count=connection.tool_count,
            purpose=request.purpose or connection.notes,
            provider=request.provider,
            result_transform=request.result_transform,
        )
    return connections
