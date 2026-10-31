"""MCP 客户端：连接服务器、列出工具、调用工具。

异步内核基于官方 mcp SDK；Ben 主体是同步架构，因此用 asyncio.run() 在外层
包一层同步函数，供 ToolSpec 的 handler 直接调用。

最简版取舍：
- 每次调用都新建连接、用完即关，不保持长会话（mcp 的会话绑定在创建它的
  事件循环上，跨 asyncio.run 复用会报错，故最简版不做缓存）；
- 单个服务器连接失败不影响其他服务器（fail-open）；
- 同时支持 stdio（本地子进程）与 http（远程端点，可带 Bearer 令牌）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from Ben.tools.mcp.config import McpConnectionConfig

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _transport(config: McpConnectionConfig):
    """按配置打开传输层，产出 (read_stream, write_stream)。"""
    if config.transport == "stdio":
        # 本地进程启动参数
        params = StdioServerParameters(
            command=config.command,  # type: ignore[arg-type]
            args=config.args,
            env=config.env or None,
        )
        # 启动本地进程
        async with stdio_client(params) as streams:
            yield streams
    else:
        headers = (
            {"Authorization": f"Bearer {config.token}"} if config.token else None
        )
        async with streamablehttp_client(
            config.url,  # type: ignore[arg-type]
            headers=headers,
        ) as streams:
            # http 传输返回三元组 (read, write, get_session_id)，只取前两个
            yield streams[0], streams[1]


@contextlib.asynccontextmanager
async def _session(config: McpConnectionConfig):
    """打开传输并建立已初始化的 MCP 会话。"""
    async with _transport(config) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    """把 mcp 的 Tool 对象转成可 JSON 序列化的普通字典。"""
    return {
        "name": tool.name,
        "description": (tool.description or "").strip() or None,
        "input_schema": tool.inputSchema or {"type": "object"},
    }


async def _list_one(config: McpConnectionConfig) -> dict[str, Any]:
    """连接一个服务器并返回其工具清单（已应用白名单）。"""
    async with _session(config) as session:
        result = await session.list_tools()
    tools = [
        _tool_to_dict(tool)
        for tool in result.tools
        if config.is_tool_allowed(tool.name)
    ]
    return {
        "name": config.name,
        "notes": config.notes,
        "tool_count": len(tools),
        "tools": tools,
    }


async def _list_all(configs: list[McpConnectionConfig]) -> list[dict[str, Any]]:
    """并发列出所有连接；单个连接失败时错误信息进 error 字段，不抛异常。"""

    async def safe_list(config: McpConnectionConfig) -> dict[str, Any]:
        try:
            return await _list_one(config)
        except Exception as exc:  # noqa: BLE001 - fail-open，错误信息回传
            logger.warning("MCP 连接 %r 列工具失败：%s", config.name, exc)
            return {
                "name": config.name,
                "notes": config.notes,
                "tool_count": 0,
                "tools": [],
                "error": f"{type(exc).__name__}: {exc}",
            }

    return await asyncio.gather(*(safe_list(config) for config in configs))


def _unwrap(exc: BaseException) -> BaseException:
    """递归解包 anyio/asyncio 的单异常 ExceptionGroup。

    异常在穿过 anyio 任务组的 __aexit__ 时会被包成 ExceptionGroup，导致
    上层的 except ValueError 接不住。单子异常时还原为内部异常；多子异常
    （真正的并发多错误）保留原 ExceptionGroup。
    """
    if isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        return _unwrap(exc.exceptions[0])
    return exc


def list_connections(configs: list[McpConnectionConfig]) -> list[dict[str, Any]]:
    """同步入口：列出所有连接的工具清单（单个连接失败不阻断）。"""
    if not configs:
        return []
    try:
        return asyncio.run(_list_all(configs))
    except BaseException as exc:  # noqa: BLE001 - 同步边界统一解包
        raise _unwrap(exc) from exc


def _extract_content(call_result: Any) -> dict[str, Any]:
    """解析 CallToolResult：提取文本内容，非文本内容转结构化字典保留。"""
    text_parts: list[str] = []
    structured: list[Any] = []
    for item in call_result.content:
        if isinstance(item, TextContent):
            text_parts.append(item.text)
        else:
            with contextlib.suppress(Exception):
                structured.append(item.model_dump())
    return {
        "is_error": bool(call_result.isError),
        "text": "\n".join(text_parts),
        "structured": structured,
    }


async def _call_one(
    config: McpConnectionConfig,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """校验白名单与工具存在性后，调用一个工具并解析真实结果。"""
    if not config.is_tool_allowed(tool):
        raise ValueError(
            f"工具 {tool!r} 不在连接 {config.name!r} 的 allowed_tools 白名单内"
        )

    async with _session(config) as session:
        catalog = await session.list_tools()
        available = {item.name for item in catalog.tools}
        if tool not in available:
            raise ValueError(
                f"连接 {config.name!r} 上不存在工具 {tool!r}"
                f"（可用：{sorted(available)}）"
            )
        result = await session.call_tool(tool, arguments or {})

    payload = _extract_content(result)
    payload["connection"] = config.name
    payload["tool"] = tool
    return payload

# 传入4参数， configs echo {"message": "hello world"}
def call_tool(
    configs: list[McpConnectionConfig],
    connection: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """同步入口：调用指定连接上的一个工具并返回真实结果。

    连接名不存在时抛 ValueError；远端执行错误在返回字典的 is_error 中体现。
    """
    target = next((c for c in configs if c.name == connection), None)
    if target is None:
        available = [c.name for c in configs]
        raise ValueError(
            f"未知 MCP 连接 {connection!r}（已配置：{available}）"
        )
    try:
        return asyncio.run(_call_one(target, tool, arguments or {}))
    except BaseException as exc:  # noqa: BLE001 - 同步边界统一解包
        raise _unwrap(exc) from exc
