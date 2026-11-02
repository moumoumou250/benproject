"""Error beacons are no-ops in Ben; exception props never carry a message."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from Ben.report.state import ReportState
from Ben.telemetry import posthog, report_error, scarf, set_scan_phase
from Ben.telemetry._common import exception_props, get_scan_phase


PRIVATE_MESSAGE = "private message that must stay on the machine"


def test_exception_props_uses_bare_name_for_builtins() -> None:
    assert exception_props(ValueError(PRIVATE_MESSAGE)) == {"exception_type": "ValueError"}


def test_exception_props_prefixes_third_party_top_level_package() -> None:
    props = exception_props(requests.exceptions.ConnectTimeout(PRIVATE_MESSAGE))
    assert props == {"exception_type": "requests.ConnectTimeout"}


def _chained(cause: BaseException | None, *, explicit: bool) -> RuntimeError:
    exc = RuntimeError("wrapped")
    if explicit:
        exc.__cause__ = cause
        exc.__suppress_context__ = True
    else:
        exc.__context__ = cause
    return exc


def test_exception_props_reports_explicit_cause() -> None:
    props = exception_props(_chained(ConnectionError(PRIVATE_MESSAGE), explicit=True))
    assert props == {"exception_type": "RuntimeError", "exception_cause": "ConnectionError"}


def test_exception_props_reports_implicit_context() -> None:
    props = exception_props(_chained(KeyError("k"), explicit=False))
    assert props["exception_cause"] == "KeyError"


def test_exception_props_ignores_suppressed_context() -> None:
    exc = _chained(None, explicit=True)
    exc.__context__ = KeyError("k")
    assert exception_props(exc) == {"exception_type": "RuntimeError"}


def test_exception_props_unwraps_exception_group() -> None:
    group = ExceptionGroup("tasks", [TimeoutError("t"), ValueError("v")])
    assert exception_props(group) == {"exception_type": "TimeoutError"}


@pytest.mark.parametrize("telemetry", [posthog, scarf])
def test_error_beacon_is_a_no_op_and_never_leaks_the_message(
    telemetry: Any,
) -> None:
    """Ben 的遥测已无害化为 no-op：不发送、不联网、不抛错。

    这条用例守两件事：调用仍然安全（签名兼容旧调用方），以及模块里
    不再存在任何发送出口（``_send``）。
    """
    set_scan_phase("sandbox_init")

    assert telemetry.error("scan_failed", RuntimeError(PRIVATE_MESSAGE)) is None
    assert not hasattr(telemetry, "_send")
    # 阶段仍被记录，供日志/诊断使用
    assert get_scan_phase() == "sandbox_init"


@pytest.mark.parametrize("telemetry", [posthog, scarf])
def test_error_beacon_without_exception_is_still_a_no_op(telemetry: Any) -> None:
    set_scan_phase("startup")

    assert telemetry.error("docker_not_installed") is None
    assert not hasattr(telemetry, "_send")
    assert get_scan_phase() == "startup"


def test_report_error_fans_out_without_touching_the_network() -> None:
    """``report_error`` 仍向两个后端转发，但两者都是 no-op。"""
    assert report_error("model_connection_failed", TimeoutError(PRIVATE_MESSAGE)) is None
    for backend in (posthog, scarf):
        assert not hasattr(backend, "_send")


@pytest.mark.parametrize("telemetry", [posthog, scarf])
def test_scan_ended_keeps_the_state_machine_without_sending(telemetry: Any) -> None:
    """``end`` 不再发事件，但仍维护 ReportState 的结束标记。"""
    state = ReportState()
    state.scan_ended_exit_reason = "budget_exceeded"

    telemetry.end(state, exit_reason="user_exit")

    # 已记录的退出原因优先，不被本次调用的默认值覆盖
    assert state.scan_ended_exit_reason == "budget_exceeded"
    assert not hasattr(telemetry, "_send")

    fresh = ReportState()
    telemetry.end(fresh, exit_reason="user_exit")
    assert fresh.scan_ended_exit_reason == "user_exit"
