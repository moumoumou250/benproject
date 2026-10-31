"""MCP 连接配置（最简版）。

描述一个可连接的 MCP 服务器：传输方式（stdio/http）、启动命令或远程地址、
可选 Bearer 令牌、可选工具白名单。字段命名与 strix 的 mcp-servers.json 保持
一致，因此同一份 JSON 配置在两个项目里语义相同。

与 strix 的区别：strix 用 pydantic 模型；Ben 目前没有 pydantic 直接依赖，
这里用标准库 dataclass 加手工校验，减少依赖面。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class McpConnectionConfig:
    """一个 MCP 服务器连接配置。

    - stdio：本地子进程，必须提供 command；
    - http：远程端点，必须提供 url，可带 Bearer 令牌。
    """

    name: str
    """连接名，作为工具命名空间，单次运行内唯一（例如 github）。"""

    transport: Literal["http", "stdio"]
    """传输方式：http 远程端点，stdio 本地子进程。"""

    url: str | None = None
    """MCP 服务器端点，http 传输必填。"""

    command: str | None = None
    """要启动的可执行文件，stdio 传输必填。"""

    args: list[str] = field(default_factory=list)
    """传给 command 的参数（仅 stdio）。"""

    env: dict[str, str] = field(default_factory=dict)
    """子进程额外环境变量（仅 stdio）。"""

    token: str | None = None
    """Bearer 令牌，以 Authorization: Bearer <token> 发送（仅 http）。"""

    allowed_tools: list[str] | None = None
    """工具白名单；None 表示暴露服务器列出的全部工具。"""

    notes: str | None = None
    """给 agent 的自由文本说明，描述这个连接是什么、怎么用。"""

    @classmethod
    def from_dict(cls, raw: object) -> McpConnectionConfig:
        """从 JSON 字典构造配置，并做严格字段校验；非法时抛 ValueError。"""
        if not isinstance(raw, dict):
            raise ValueError("配置项必须是 JSON 对象")

        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("缺少有效的 name 字段")

        transport = raw.get("transport", "http")
        if transport not in ("http", "stdio"):
            raise ValueError(f"transport 仅支持 http/stdio，收到：{transport!r}")

        url = raw.get("url")
        if url is not None and not isinstance(url, str):
            raise ValueError("url 必须是字符串")
        command = raw.get("command")
        if command is not None and not isinstance(command, str):
            raise ValueError("command 必须是字符串")

        if transport == "http" and not url:
            raise ValueError("http 连接必须提供 url")
        if transport == "stdio" and not command:
            raise ValueError("stdio 连接必须提供 command")

        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("args 必须是字符串列表")

        env = raw.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError("env 必须是字符串到字符串的对象")

        # 仅支持 Bearer 认证，解析 auth: {"kind": "bearer", "token": "..."}
        token: str | None = None
        auth = raw.get("auth")
        if auth is not None:
            if not isinstance(auth, dict) or auth.get("kind") != "bearer":
                raise ValueError(
                    'auth 仅支持 {"kind": "bearer", "token": "..."} 形式'
                )
            token = auth.get("token")
            if not isinstance(token, str) or not token:
                raise ValueError("bearer auth 缺少有效 token")

        allowed = raw.get("allowed_tools")
        if allowed is not None and (
            not isinstance(allowed, list) or not all(isinstance(t, str) for t in allowed)
        ):
            raise ValueError("allowed_tools 必须是字符串列表")

        notes = raw.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise ValueError("notes 必须是字符串")

        return cls(
            name=name.strip(),
            transport=transport,
            url=url,
            command=command,
            args=list(args),
            env=dict(env),
            token=token,
            allowed_tools=list(allowed) if allowed is not None else None,
            notes=notes,
        )

    def is_tool_allowed(self, tool_name: str) -> bool:
        """工具是否在白名单内；未配置白名单时全部允许。"""
        return self.allowed_tools is None or tool_name in self.allowed_tools
