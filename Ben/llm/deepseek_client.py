"""DeepSeek 模型调用封装（OpenAI 兼容接口）。

- 通过环境变量/配置传入 API Key，源码中不硬编码密钥；
- 模型名、API 地址、超时均可配置；
- 能发送 Prompt 与任务上下文（messages），读取模型响应；
- 支持 OpenAI 风格的 function calling（tools），返回结构化的工具调用；
- 明确处理超时、连接错误、鉴权错误、限流、错误请求与无效响应；
- 通过 :class:`LLMClient` 协议保留后续增加其他模型适配器的可能。

第三步（本文件）先用普通文本请求验证；function calling 在第四步验证。
DeepSeek 官方文档：https://api-docs.deepseek.com/（接口与 OpenAI 兼容）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import OpenAI

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 自定义异常：把底层 SDK 的错误转换为明确、可处理的类型
# ----------------------------------------------------------------------
class LLMError(RuntimeError):
    """模型调用相关错误的基类。"""


class LLMTimeoutError(LLMError):
    """请求超时。"""


class LLMConnectionError(LLMError):
    """无法连接到模型服务。"""


class LLMAuthError(LLMError):
    """API Key 无效或未授权。"""


class LLMRateLimitError(LLMError):
    """触发限流。"""


class LLMRequestError(LLMError):
    """请求本身不合法（如参数格式、模型名错误）。"""


class LLMInvalidResponseError(LLMError):
    """模型返回了无法解析的响应。"""


@dataclass(frozen=True)
class ToolCall:
    """模型提出的一次工具调用（仅为请求，是否执行由程序端决定）。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    """模型响应：文本 + 结构化工具调用 + 结束原因。"""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    raw: Any = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLMClient(Protocol):
    """模型客户端协议：后续新增模型适配器实现该接口即可。"""

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = ...) -> LLMResponse:
        ...


class DeepSeekClient:
    """DeepSeek（OpenAI 兼容）同步客户端。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise LLMAuthError("创建 DeepSeek 客户端需要 API Key")
        self.model = model
        self.base_url = base_url
        # OpenAI 兼容：DeepSeek 接受相同的请求格式与鉴权方式（Bearer Key）。
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: Any = "auto",
    ) -> LLMResponse:
        """发送一次对话请求。

        :param messages: OpenAI 风格消息列表 [{"role": ..., "content": ...}]
        :param tools: OpenAI 风格工具定义（function schema），None 表示纯文本
        :param tool_choice: "auto" 自动；"none" 不调用；或函数对象
            ``{"type":"function","function":{"name":...}}`` 强制调用指定工具
        :return: :class:`LLMResponse`
        """
        if not messages:
            raise LLMRequestError("messages 不能为空")

        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        logger.info("调用模型 %s（messages=%d, tools=%d）", self.model, len(messages), len(tools or []))
        try:
            completion = self._client.chat.completions.create(**kwargs)
        except _openai_timeout() as exc:
            raise LLMTimeoutError(f"模型请求超时：{exc}") from exc
        except _openai_connection() as exc:
            raise LLMConnectionError(f"无法连接模型服务：{exc}") from exc
        except _openai_auth() as exc:
            raise LLMAuthError(f"模型鉴权失败（检查 API Key）：{exc}") from exc
        except _openai_rate_limit() as exc:
            raise LLMRateLimitError(f"模型限流：{exc}") from exc
        except _openai_bad_request() as exc:
            raise LLMRequestError(f"模型请求被拒绝：{exc}") from exc
        except _openai_base() as exc:
            raise LLMError(f"模型调用失败：{exc}") from exc

        return self._parse(completion)

    @staticmethod
    def _parse(completion: Any) -> LLMResponse:
        """解析 SDK 返回，校验结构，提取文本与工具调用。"""
        if completion is None or not getattr(completion, "choices", None):
            raise LLMInvalidResponseError("模型响应中没有 choices")
        choice = completion.choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise LLMInvalidResponseError("模型响应中没有 message")

        text = (getattr(message, "content", None) or "").strip()
        tool_calls: list[ToolCall] = []

        raw_calls = getattr(message, "tool_calls", None) or []
        for raw in raw_calls:
            func = getattr(raw, "function", None)
            name = getattr(func, "name", "") if func else ""
            raw_args = getattr(func, "arguments", "") if func else ""
            call_id = getattr(raw, "id", "") or f"call_{name}"
            if not name:
                raise LLMInvalidResponseError("工具调用缺少函数名")
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as exc:
                raise LLMInvalidResponseError(
                    f"工具 {name} 的参数不是合法 JSON：{raw_args!r}"
                ) from exc
            if not isinstance(arguments, dict):
                raise LLMInvalidResponseError(f"工具 {name} 的参数必须是 JSON 对象")
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))

        finish_reason = getattr(choice, "finish_reason", None)
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw=completion,
        )


# ----------------------------------------------------------------------
# 延迟获取 openai 异常类（避免在没有 openai 时导入即失败）
# ----------------------------------------------------------------------
def _openai_timeout() -> tuple[type[BaseException], ...]:
    from openai import APITimeoutError

    return (APITimeoutError,)


def _openai_connection() -> tuple[type[BaseException], ...]:
    from openai import APIConnectionError

    return (APIConnectionError,)


def _openai_auth() -> tuple[type[BaseException], ...]:
    from openai import AuthenticationError

    return (AuthenticationError,)


def _openai_rate_limit() -> tuple[type[BaseException], ...]:
    from openai import RateLimitError

    return (RateLimitError,)


def _openai_bad_request() -> tuple[type[BaseException], ...]:
    from openai import BadRequestError

    return (BadRequestError,)


def _openai_base() -> tuple[type[BaseException], ...]:
    from openai import APIError

    return (APIError,)
