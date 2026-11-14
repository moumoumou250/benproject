"""Scarf 遥测的 Ben 无害化版本：全部函数为 no-op（仅 debug 日志）。

与 strix 公开函数签名一致；``end`` 仍维护 ReportState 的扫描结束状态字段，
不发起任何网络请求。
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
    logger.debug("telemetry(scarf) disabled: scan_started")


def finding(severity: str, cwe: str | None = None, is_cve: bool = False) -> None:
    logger.debug("telemetry(scarf) disabled: finding_reported (%s)", severity)


def end(report_state: ReportState, exit_reason: str = "completed") -> None:
    """维护 ReportState 的扫描结束标记；不发送任何遥测事件。"""
    if report_state.scarf_scan_ended_sent:
        return
    if report_state.scan_ended_exit_reason is None:
        report_state.scan_ended_exit_reason = exit_reason
    report_state.scarf_scan_ended_sent = True
    logger.debug("telemetry(scarf) disabled: scan_ended (%s)", exit_reason)


def error(error_type: str, exc: BaseException | None = None) -> None:
    logger.debug("telemetry(scarf) disabled: error beacon (%s)", error_type)
