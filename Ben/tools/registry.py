"""最小工具注册与校验机制。

职责（对应需求第三点）：
1. 定义可用工具：名称、用途、参数结构（JSON Schema）；
2. 生成给模型看的 OpenAI 风格工具描述（function schema）；
3. 接收模型提出的工具调用，先验证工具名称，再用 JSON Schema 严格校验参数；
4. 把调用交给对应工具的执行器（handler），由其在受控环境中真实执行；
5. 返回真实执行结果。

模型返回的任何文本都不会被直接当作 shell 命令：工具名必须已注册，参数必须
通过 schema 与工具自身的安全校验，命令由执行器以参数数组形式构造。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import Draft202012Validator

from Ben.runtime.docker_executor import DockerExecutor

logger = logging.getLogger(__name__)


class ToolError(RuntimeError):
    """工具层错误基类。"""


class ToolNotFoundError(ToolError):
    """请求了未注册的工具。"""


class ToolValidationError(ToolError):
    """工具参数未通过校验。"""


@dataclass(frozen=True)
class ToolRuntime:
    """工具执行时的运行时依赖（由 Agent 注入，避免工具自己 new 执行器）。"""

    executor: DockerExecutor
    allowed_targets: tuple[str, ...]
    command_timeout: float = 30.0


@dataclass(frozen=True)
class ToolResult:
    """工具真实执行结果（返回给模型作为下一步决策依据）。"""

    name: str
    ok: bool
    # 给模型阅读的简洁文本（必须基于真实执行结果）
    summary: str
    # 结构化数据（用于报告/日志）
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_message(self) -> str:
        """转为可放入对话的工具结果文本。"""
        header = f"[工具 {self.name} 执行{'成功' if self.ok else '失败'}]"
        return f"{header}\n{self.summary if self.ok else self.error}"


# 工具处理函数：接收（已校验的参数, 运行时），返回 ToolResult
ToolHandler = Callable[[dict[str, Any], ToolRuntime], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的静态定义 + 执行器。"""
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def openai_schema(self) -> dict[str, Any]:
        """生成 OpenAI 风格的 function 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        # _tools是属性
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ToolError(f"工具已存在：{spec.name}")
        # 提前校验参数 schema 本身合法，避免把坏 schema 发给模型。
        Draft202012Validator.check_schema(spec.parameters)
        # 此处为tool与spec的映射关系
        self._tools[spec.name] = spec
        logger.info("注册工具：%s", spec.name)
    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolNotFoundError(f"未注册的工具：{name}（可用：{sorted(self._tools)}）")
        return spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        """返回所有工具的 OpenAI 描述，供模型 function calling 使用。"""
        return [spec.openai_schema() for spec in self._tools.values()]

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        """用 JSON Schema 严格校验参数。"""
        spec = self.get(name)
        if not isinstance(arguments, dict):
            raise ToolValidationError(f"工具 {name} 的参数必须是 JSON 对象")
        validator = Draft202012Validator(spec.parameters)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: e.path)
        if errors:
            first = errors[0]
            loc = ".".join(str(p) for p in first.absolute_path) or "<root>"
            raise ToolValidationError(f"工具 {name} 参数非法（{loc}）：{first.message}")
    # tc.name = "call_mcp" 
    # arguments是dict，内容为{"connection": "filesystem", "tool": "echo", "arguments": {"message": "ok"}}}
    def execute(self, name: str, arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
        # 
        spec = self.get(name)
        self.validate_arguments(name, arguments)
        logger.info("执行工具 %s，参数：%s", name, _redact(arguments))
        try:
            return spec.handler(arguments, runtime)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - 工具内部异常转为明确错误
            raise ToolError(f"工具 {name} 执行异常：{type(exc).__name__}: {exc}") from exc


def _redact(arguments: dict[str, Any]) -> dict[str, Any]:
    """记录日志时对可能敏感的字段做脱敏。"""
    redacted = dict(arguments)
    for key in ("authorization", "token", "cookie", "api_key"):
        if key in redacted:
            redacted[key] = "***"
    return redacted


def build_default_registry() -> ToolRegistry:  # 预期返回ToolRegistry类型对象
    """注册默认工具：基础 HTTP 检查 + MCP 发现/调度 + 只读知识片段加载。"""
    from Ben.tools.http_check import HTTP_CHECK_SPEC
    from Ben.tools.load_skill import LOAD_SKILL_SPEC
    from Ben.tools.mcp import CALL_MCP_SPEC, LIST_MCP_SPEC
    # registry是ToolRegistry()类的实例，他的实例属性只有`_tools`
    # registry.__dict__ == {'_tools': {}}
    registry = ToolRegistry()
    # HTTP_CHECK_SPEC是ToolSpec的实例对象
    """
    注册前:registry.__dict__ == {'_tools': {}}
    注册后:registry.__dict__ == {
        'http_check': HTTP_CHECK_SPEC,
        'list_mcps': LIST_MCP_SPEC,
        'call_mcp': CALL_MCP_SPEC,
        'load_skill': LOAD_SKILL_SPEC,
    }
    """
    registry.register(HTTP_CHECK_SPEC)
    # 只读知识片段加载：按名读取已注册片段目录，不接受路径
    registry.register(LOAD_SKILL_SPEC)
    # MCP 工具：无配置文件时 list_mcps 返回空，不影响现有任务流程
    registry.register(LIST_MCP_SPEC)
    """
    # 此处 CALL_MCP_SPEC 已提前注册到 registry 中
    # 当 self.registry.execute() 接收到 tc.name = "call_mcp" 时
    # 会通过 name 找到 CALL_MCP_SPEC 实例
    # 再通过 CALL_MCP_SPEC.handler 调用 _call_mcp 函数
    """
    registry.register(CALL_MCP_SPEC)


    return registry
"""

"""