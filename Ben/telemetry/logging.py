"""扫描范围日志（移植自 strix 的 ``telemetry/logging.py``）。

提供两层：
- :func:`setup_console_logging` —— 进程级：只往 stderr 输出，默认 ERROR，
  抑制第三方噪音；供 CLI 入口调用。
- :func:`setup_scan_logging` —— 扫描级：额外把 DEBUG 全量写进
  ``{run_dir}/ben.log``，并给每条记录打上 ``scan_id``/``agent_id``；
  返回 teardown，供 ``finally`` 调用。

与 strix 的差异：日志器根名 ``Ben``、日志文件 ``ben.log``、DEBUG 开关
``BEN_DEBUG``；其余语义（幂等 teardown、依赖降噪、unraisable 钩子）保持一致。
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import sys
import warnings
from contextvars import ContextVar
from pathlib import Path  # noqa: TC003 - setup_scan_logging 运行期需要
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = [
    "configure_dependency_logging",
    "set_agent_id",
    "set_scan_id",
    "setup_console_logging",
    "setup_scan_logging",
]


_SCAN_ID: ContextVar[str | None] = ContextVar("ben_scan_id", default=None)
_AGENT_ID: ContextVar[str | None] = ContextVar("ben_agent_id", default=None)


def set_scan_id(scan_id: str) -> None:
    """设置此后本 asyncio 任务树中每条日志可见的 scan_id。"""
    _SCAN_ID.set(scan_id)


def set_agent_id(agent_id: str | None) -> None:
    """设置/清空此后日志中的 agent_id（``None`` 渲染为 ``-``）。"""
    _AGENT_ID.set(agent_id)


class _BenContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.scan_id = _SCAN_ID.get() or "-"
        record.agent_id = _AGENT_ID.get() or "-"
        return True


_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(scan_id)s %(agent_id)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# DEBUG 时噪音过大的第三方日志器。
_NOISY_LIBS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "litellm",
    "openai",
    "anthropic",
)

_HANDLER_TAG = "_ben_scan_handler"
_STREAM_TAG = "_ben_stream_handler"

# ``openai.agents`` 是 openai-agents SDK 的标准日志器根。
_TRACKED_ROOTS: tuple[str, ...] = ("Ben", "openai.agents")

_STDOUT_QUIET_ROOTS: frozenset[str] = frozenset({"openai.agents"})


class _StderrQuietFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not any(
            record.name == root or record.name.startswith(root + ".")
            for root in _STDOUT_QUIET_ROOTS
        )


class _PtyThresholdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "PTY process count reached warning threshold" not in record.getMessage()


@functools.cache  # 只装一次；测试用 cache_clear() 复位
def _silence_pty_threshold_warning() -> None:
    logging.getLogger("agents.sandbox.sandboxes.docker").addFilter(_PtyThresholdFilter())


def configure_dependency_logging() -> None:
    """压低会淹没扫描日志的依赖日志与告警。"""
    litellm = sys.modules.get("litellm")
    if litellm is not None:
        with contextlib.suppress(Exception):
            litellm._logging._disable_debugging()

    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    logging.getLogger("asyncio").propagate = False
    warnings.filterwarnings("ignore", category=RuntimeWarning, module="asyncio")
    _route_unraisable_to_log()
    _silence_pty_threshold_warning()


def _route_unraisable_to_log() -> None:
    def hook(u: sys.UnraisableHookArgs) -> None:
        with contextlib.suppress(BaseException):
            logger = logging.getLogger("Ben.telemetry")
            if logger.hasHandlers():
                logger.warning(
                    u.err_msg or f"Exception ignored in {u.object!r}",
                    exc_info=(u.exc_type, u.exc_value, u.exc_traceback),  # type: ignore[arg-type]
                )

    sys.unraisablehook = hook


class _CurrentStderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """始终写当前 ``sys.stderr``，捕获重定向（pytest capsys / 管道）。"""

    def __init__(self, level: int) -> None:
        logging.Handler.__init__(self, level)

    @property
    def stream(self) -> object:
        return sys.stderr

    @stream.setter
    def stream(self, _value: object) -> None:
        pass


def _debug_enabled(debug: bool | None) -> bool:
    if debug is not None:
        return debug
    return (os.environ.get("BEN_DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}


def _stream_handler(debug: bool) -> logging.Handler:
    handler = _CurrentStderrHandler(logging.DEBUG if debug else logging.ERROR)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handler.addFilter(_BenContextFilter())
    handler.addFilter(_StderrQuietFilter())
    setattr(handler, _STREAM_TAG, True)
    return handler


def _has_stream_handler(tracked: logging.Logger) -> bool:
    return any(getattr(handler, _STREAM_TAG, False) for handler in tracked.handlers)


def setup_console_logging(*, debug: bool | None = None) -> None:
    """进程级日志：只挂 stderr（默认 ERROR），幂等。"""
    configure_dependency_logging()
    for name in _TRACKED_ROOTS:
        tracked = logging.getLogger(name)
        tracked.setLevel(logging.DEBUG)
        tracked.propagate = False
        if not _has_stream_handler(tracked):
            tracked.addHandler(_stream_handler(_debug_enabled(debug)))


def setup_scan_logging(run_dir: Path, *, debug: bool | None = None) -> Callable[[], None]:
    """挂扫描级日志处理器，返回 teardown 可调用对象。

    Args:
        run_dir: 单次扫描的输出目录；``{run_dir}/ben.log`` 以追加模式打开
            （同一 run 名重跑时自然续写）。
        debug: ``True`` 时 stderr 走 DEBUG 而非 ERROR；``None`` 读
            ``BEN_DEBUG`` 环境变量。

    Returns:
        无参可调用对象，flush/close 并摘掉本次挂上的处理器；幂等，
        可安全放在 ``finally`` 里。
    """
    configure_dependency_logging()

    debug = _debug_enabled(debug)

    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "ben.log"

    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    file_handler.addFilter(_BenContextFilter())
    setattr(file_handler, _HANDLER_TAG, True)

    tracked_loggers = [logging.getLogger(name) for name in _TRACKED_ROOTS]
    for tracked in tracked_loggers:
        tracked.setLevel(logging.DEBUG)
        tracked.addHandler(file_handler)
        if not _has_stream_handler(tracked):
            stream_handler = _stream_handler(debug)
            setattr(stream_handler, _HANDLER_TAG, True)
            tracked.addHandler(stream_handler)
        tracked.propagate = False

    for name in _NOISY_LIBS:
        logging.getLogger(name).setLevel(logging.WARNING)

    def _teardown() -> None:
        for tracked in tracked_loggers:
            for handler in list(tracked.handlers):
                if getattr(handler, _HANDLER_TAG, False):
                    tracked.removeHandler(handler)
                    with contextlib.suppress(Exception):
                        handler.flush()
                        handler.close()

    return _teardown
