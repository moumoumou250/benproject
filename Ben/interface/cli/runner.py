"""headless 模式的 Rich 实时视图（移植自 strix 的 ``interface/cli.py``）。

与 strix 的差异（Ben 架构约束，刻意保留）：
- strix 调用 ``core/runner.run_strix_scan(scan_config=...)``；Ben 的编排入口是
  :class:`Ben.core.runner.AgentRunner`，这里按其构造签名组装，并复用同一份
  ``ReportState``（与 TUI 一致：界面与执行器看到同一状态）。
- 上游在 ``signal.signal(SIGINT/SIGTERM/SIGHUP)`` 里 ``sys.exit`` 收尾；Ben 保留
  自身退出码语义（Ctrl-C → 130 由 :mod:`Ben.interface.main` 决定），这里只用
  ``try/finally`` 做幂等清理。
- 面板文案改为中文，与 Ben 其余输出一致。

调用方：:func:`Ben.interface.main._run_task`（脚本/CI 的 headless 路径）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from Ben.config import load_settings
from Ben.interface.terminal.stats import (
    build_final_stats_text,
    build_live_stats_text,
    format_vulnerability_report,
    has_model_response,
)
from Ben.report.state import ReportState, set_global_report_state


if TYPE_CHECKING:
    import argparse

    from Ben.core.runner import AgentRunResult


logger = logging.getLogger(__name__)

__all__ = ["run_cli"]

_BRAND = "BEN"
_TITLE_STYLE = "bold white"
_OK_STYLE = "#22c55e"
_INFO_STYLE = "#60a5fa"

_REFRESH_SECONDS = 2.0


def _build_scan_config(args: argparse.Namespace, prepared: dict[str, Any]) -> dict[str, Any]:
    """与 ``GoTuiRuntime.init_run_state`` 保持同构的 scan_config。"""
    return {
        "scan_id": prepared["run_name"],
        "targets": prepared["targets_info"],
        "user_instructions": prepared["instruction"] or "",
        "run_name": prepared["run_name"],
        "diff_scope": prepared["diff_scope"],
        "scan_mode": getattr(args, "scan_mode", "deep"),
        "non_interactive": True,
        "local_sources": prepared["local_sources"] or [],
        "workspace_files": prepared["workspace_files"] or [],
        "scope_mode": getattr(args, "scope_mode", "auto"),
        "diff_base": getattr(args, "diff_base", None),
        "resume_instruction": getattr(args, "user_explicit_instruction", None) or "",
    }


def _target_text(targets_info: list[dict[str, Any]]) -> Text:
    text = Text()
    text.append("目标", style="dim")
    text.append("  ")
    if not targets_info:
        text.append("（白名单首项）", style="white")
        return text
    if len(targets_info) == 1:
        text.append(str(targets_info[0].get("original", "")), style="bold white")
        return text
    text.append(f"{len(targets_info)} 个目标", style="bold white")
    for target_info in targets_info:
        text.append("\n        ")
        text.append(str(target_info.get("original", "")), style="white")
    return text


def _panel(content: Text, *, border_style: str = _OK_STYLE) -> Panel:
    return Panel(
        content,
        title=f"[{_TITLE_STYLE}]{_BRAND}[/]",
        title_align="left",
        border_style=border_style,
        padding=(1, 2),
    )


def _display_vulnerability(
    console: Console, report: dict[str, Any], *, updated: bool = False
) -> None:
    report_id = str(report.get("id", "unknown"))
    suffix = "（已更新）" if updated else ""
    console.print(
        Panel(
            format_vulnerability_report(report),
            title=f"[bold red]{report_id.upper()}{suffix}",
            title_align="left",
            border_style="red",
            padding=(1, 2),
        )
    )
    console.print()


def _display_vulnerability_deleted(console: Console, report: dict[str, Any]) -> None:
    report_id = str(report.get("id", "unknown"))
    deletion = report.get("deletion")
    deletion = deletion if isinstance(deletion, dict) else {}
    deleted_by = deletion.get("agent_name") or deletion.get("agent_id") or "agent"

    text = Text()
    text.append("已撤回：", style="bold")
    text.append(f"{report.get('title', '')}\n\n")
    text.append(f"撤回者 {deleted_by}。", style="dim")
    text.append(str(deletion.get("reason") or ""))

    console.print(
        Panel(
            text,
            title=f"[bold yellow]{report_id.upper()}（撤回）",
            title_align="left",
            border_style="yellow",
            padding=(1, 2),
        )
    )
    console.print()


def _live_panel(report_state: ReportState, startup_phase: list[str]) -> Panel:
    status_text = Text()
    status_text.append("正在进行安全测试", style=f"bold {_OK_STYLE}")
    status_text.append("\n\n")

    if not has_model_response(report_state):
        status_text.append(f"{startup_phase[0]}...", style="dim")
        status_text.append("\n\n")

    stats_text = build_live_stats_text(report_state)
    if stats_text.plain:
        status_text.append(stats_text)

    return _panel(status_text)


def _final_panel(report_state: ReportState) -> Panel | None:
    final_report = report_state.final_scan_result
    if not final_report:
        return None

    content = Text()
    content.append("测试总结", style=f"bold {_INFO_STYLE}")
    content.append("\n\n")
    content.append(final_report)

    stats_text = build_final_stats_text(report_state)
    if stats_text.plain:
        content.append("\n\n")
        content.append(stats_text)

    return _panel(content, border_style=_INFO_STYLE)


def _spawn_status_thread(
    live_display: Live, report_state: ReportState, startup_phase: list[str]
) -> threading.Event:
    stop = threading.Event()

    def update_status() -> None:
        while not stop.is_set():
            try:
                live_display.update(_live_panel(report_state, startup_phase))
            except Exception:  # noqa: BLE001 - 渲染异常不应中断扫描
                break
            stop.wait(_REFRESH_SECONDS)

    threading.Thread(target=update_status, daemon=True, name="ben-live-status").start()
    return stop


def _safe_cleanup(report_state: ReportState, status: str = "stopped") -> None:
    with contextlib.suppress(Exception):
        report_state.cleanup(status=status)


def _setup_scan_logging(args: argparse.Namespace, report_state: ReportState) -> Any:
    """挂扫描级日志（``strix_runs/<run>/ben.log``）；失败时返回无害 no-op。"""
    from Ben.telemetry.logging import setup_scan_logging

    try:
        run_dir = report_state.get_run_dir()
        return setup_scan_logging(run_dir, debug=bool(getattr(args, "verbose", False)))
    except Exception:  # noqa: BLE001 - 日志初始化失败不影响主流程
        logger.debug("扫描日志初始化失败", exc_info=True)
        return lambda: None


async def run_cli(
    args: argparse.Namespace,
    prepared: dict[str, Any],
    *,
    live: bool = True,
) -> AgentRunResult | None:
    """跑完整 Agent 任务并实时展示进度。

    Returns:
        最终结果；预算耗尽/被取消时为 ``None``（报告已落盘，摘要照常输出）。
    """
    from Ben.core.hooks import BudgetExceededError
    from Ben.core.runner import AgentRunner

    console = Console()
    run_name = prepared["run_name"]
    targets_info = prepared["targets_info"] or []
    scan_config = _build_scan_config(args, prepared)
    startup_phase: list[str] = ["启动中"]

    console.print()
    start_text = Text()
    start_text.append("安全测试已启动", style=f"bold {_OK_STYLE}")
    start_text.append("\n\n")
    start_text.append(_target_text(targets_info))
    start_text.append("\n")
    start_text.append("输出", style="dim")
    start_text.append("  ")
    start_text.append(f"strix_runs/{run_name}", style=_INFO_STYLE)
    start_text.append("\n\n")
    start_text.append("漏洞将在发现时实时展示。", style="dim")
    console.print(_panel(start_text))
    console.print()

    report_state = ReportState(run_name)
    report_state.hydrate_from_run_dir()
    report_state.set_scan_config(scan_config)
    report_state.save_run_data()
    set_global_report_state(report_state)

    report_state.vulnerability_found_callback = lambda report: _display_vulnerability(
        console, report
    )
    report_state.vulnerability_updated_callback = lambda report: _display_vulnerability(
        console, report, updated=True
    )
    report_state.vulnerability_deleted_callback = lambda report: _display_vulnerability_deleted(
        console, report
    )

    teardown_scan_logging = _setup_scan_logging(args, report_state)

    settings = load_settings(require_api_key=True)
    runner = AgentRunner(
        settings=settings,
        local_sources=prepared["local_sources"] or None,
        targets_info=targets_info or None,
        scan_mode=getattr(args, "scan_mode", "deep"),
        user_instructions=prepared["instruction"],
        diff_scope=prepared["diff_scope"],
        interactive=bool(getattr(args, "interactive", False)),
        resume_run_name=prepared.get("resume_run_name"),
        resume_instruction=getattr(args, "user_explicit_instruction", None),
        max_turns=getattr(args, "max_turns", None),
        max_budget_usd=getattr(args, "max_budget_usd", None),
        run_name=None if prepared.get("resume_run_name") else run_name,
        workspace_files=prepared["workspace_files"] or None,
        report_state=report_state,
    )

    result: AgentRunResult | None = None
    try:
        if live:
            with Live(
                _live_panel(report_state, startup_phase),
                console=console,
                refresh_per_second=2,
                transient=False,
            ) as live_display:
                stop_updates = _spawn_status_thread(live_display, report_state, startup_phase)
                try:
                    result = await runner.run_async()
                finally:
                    stop_updates.set()
        else:
            result = await runner.run_async()
    except (asyncio.CancelledError, BudgetExceededError):
        # 预算到顶/被取消不是错误：报告已落盘，摘要照常输出。
        logger.info("扫描提前结束（预算或取消）")
        _safe_cleanup(report_state)
    except KeyboardInterrupt:
        _safe_cleanup(report_state, status="interrupted")
        raise
    finally:
        with contextlib.suppress(Exception):
            teardown_scan_logging()

    final_panel = _final_panel(report_state)
    if final_panel is not None:
        console.print()
        console.print(final_panel)
        console.print()

    return result
