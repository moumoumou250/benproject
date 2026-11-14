"""启动自检记下的路由决定：端点不接受 OpenAI 时不再走 OpenAI 客户端。

扫描开始前的最小往返若得到「没有这个 OpenAI 接口」一类的拒绝，
:func:`mark_skip_openai` 会让后续模型解析改走 chat completions，
而不是继续用 OpenAI Responses 流式把每个回合打成
``Error streaming response``。
"""

from __future__ import annotations

_skip_openai = False
_probed_model: str | None = None

# 400 只有带这些字样才算「不支持」；裸 400 经常是参数问题，接口本身是通的。
_UNSUPPORTED_MARKERS = (
    "not supported",
    "unsupported",
    "unknown url",
    "unrecognized request url",
    "invalid url",
    "no such endpoint",
    "does not exist",
    "streaming is not supported",
    "does not support",
)

# 这些状态码表示路径本身不存在，不必再看正文。
_MISSING_ENDPOINT_STATUSES = frozenset({404, 405, 501})


def skip_openai() -> bool:
    """True 时 Grok 订阅不再使用 OpenAI Responses 客户端。"""
    return _skip_openai


def mark_skip_openai() -> None:
    global _skip_openai
    _skip_openai = True


def was_probed(model: str) -> bool:
    return _probed_model == model


def mark_probed(model: str) -> None:
    global _probed_model
    _probed_model = model


def reset() -> None:
    """测试隔离用。"""
    global _skip_openai, _probed_model
    _skip_openai = False
    _probed_model = None


def is_openai_unsupported(exc: BaseException) -> bool:
    """端点明确拒绝 OpenAI Responses / streaming，而不是网络或鉴权失败。"""
    from Ben.config import codex, grok_build

    if grok_build.is_privacy_mode_error(exc) or codex.is_content_guardrail_error(exc):
        return False
    current: BaseException | None = exc
    seen: list[BaseException] = []
    while current is not None and current not in seen:
        seen.append(current)
        status = getattr(current, "status_code", None)
        text = str(current).lower()
        if isinstance(status, int) and status in _MISSING_ENDPOINT_STATUSES:
            return True
        if any(marker in text for marker in _UNSUPPORTED_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False
