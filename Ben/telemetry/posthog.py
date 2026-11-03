"""PostHog 遥测的 Ben 无害化版本：全部函数为 no-op（仅 debug 日志）。

保留与 strix 完全一致的公开函数签名，使调用方（report/state、core/execution
等）无需改动。``end`` 仍维护 ReportState 上的扫描结束状态字段（退出原因、
发送标记），保证状态机语义不变；不发起任何网络请求。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from Ben.report.state import ReportState


logger = logging.getLogger(__name__)


def start(
    model: str | None,
    scan_mode: str | None,
    is_whitebox: bool,
    interactive: bool,
    has_instructions: bool,
    auth_mode: str | None = None,
) -> None:
    logger.debug("telemetry(posthog) disabled: scan_started")


def finding(severity: str, cwe: str | None = None, is_cve: bool = False) -> None:
    logger.debug("telemetry(posthog) disabled: finding_reported (%s)", severity)


def end(report_state: ReportState, exit_reason: str = "completed") -> None:
    """维护 ReportState 的扫描结束标记；不发送任何遥测事件。"""
    if report_state.posthog_scan_ended_sent:
        return
    if report_state.scan_ended_exit_reason is None:
        report_state.scan_ended_exit_reason = exit_reason
    report_state.posthog_scan_ended_sent = True
    logger.debug("telemetry(posthog) disabled: scan_ended (%s)", exit_reason)


def viewer_opened(source: str, live: bool) -> None:
    logger.debug("telemetry(posthog) disabled: viewer_opened")


def viewer_cta_clicked(cta: str, surface: str | None = None) -> None:
    logger.debug("telemetry(posthog) disabled: viewer_cta_clicked")


_VIEWER_EMAIL_STEPS = frozenset(
    {"email_submitted", "email_verified", "report_sent", "work_email_required"}
)


def viewer_email_event(step: str, purpose: str | None = None) -> None:
    if step not in _VIEWER_EMAIL_STEPS:
        return
    logger.debug("telemetry(posthog) disabled: viewer_%s", step)


def viewer_feedback_submitted() -> None:
    logger.debug("telemetry(posthog) disabled: viewer_feedback_submitted")


def viewer_agent_steered() -> None:
    logger.debug("telemetry(posthog) disabled: viewer_agent_steered")


def error(error_type: str, exc: BaseException | None = None) -> None:
    logger.debug("telemetry(posthog) disabled: error beacon (%s)", error_type)
