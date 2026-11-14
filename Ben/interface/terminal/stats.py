"""运行时统计与漏洞面板的 Rich 渲染（移植自 strix 的 ``interface/utils.py``）。

职责边界：
- strix 把「目标解析」与「展示/统计」混在同一个 ``interface/utils.py``（1,790 行）；
- Ben 已把目标解析拆到 :mod:`Ben.interface.shared.targets`，本模块只承接展示层，
  不改动 ``targets.py``，避免覆盖式移植冲掉授权白名单相关逻辑。

本模块为纯展示函数：只读 ``ReportState``，不产生任何副作用，
任何异常都不应改变扫描结果（调用方负责容错）。
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

from Ben.config import load_settings


__all__ = [
    "build_final_stats_text",
    "build_live_stats_text",
    "build_tui_stats_text",
    "format_token_count",
    "format_vulnerability_report",
    "get_cvss_color",
    "get_severity_color",
    "has_model_response",
    "is_subscription_run",
]


_SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "info")

_SEVERITY_COLORS: dict[str, str] = {
    "critical": "#dc2626",
    "high": "#ea580c",
    "medium": "#d97706",
    "low": "#65a30d",
    "info": "#0284c7",
}

_FIELD_STYLE = "bold #4ade80"


def get_severity_color(severity: str) -> str:
    return _SEVERITY_COLORS.get(severity, "#6b7280")


def get_cvss_color(cvss_score: float) -> str:
    if cvss_score >= 9.0:
        return "#dc2626"
    if cvss_score >= 7.0:
        return "#ea580c"
    if cvss_score >= 4.0:
        return "#d97706"
    if cvss_score >= 0.1:
        return "#65a30d"
    return "#6b7280"


def format_token_count(count: float | None) -> str:
    value = int(count or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _cvss_vector(report: dict[str, Any]) -> str:
    """把 ``cvss_breakdown`` 渲染成 ``AV:N/AC:L/...`` 形式的向量串。"""
    breakdown = report.get("cvss_breakdown") or {}
    if not isinstance(breakdown, dict):
        return ""
    parts: list[str] = []
    for key, label in (
        ("attack_vector", "AV"),
        ("attack_complexity", "AC"),
        ("privileges_required", "PR"),
        ("user_interaction", "UI"),
        ("scope", "S"),
        ("confidentiality", "C"),
        ("integrity", "I"),
        ("availability", "A"),
    ):
        value = breakdown.get(key)
        if value:
            parts.append(f"{label}:{value}")
    return "/".join(parts)


def _append_field(text: Text, label: str, value: str) -> None:
    text.append("\n\n")
    text.append(f"{label}：", style=_FIELD_STYLE)
    text.append(value)


def format_vulnerability_report(report: dict[str, Any]) -> Text:  # noqa: PLR0912, PLR0915
    """把一条漏洞记录渲染成 Rich ``Text``（实时面板用）。"""
    text = Text()

    title = report.get("title", "")
    if title:
        text.append("漏洞报告", style="bold #ea580c")
        _append_field(text, "标题", str(title))

    severity = report.get("severity", "")
    if severity:
        text.append("\n\n")
        text.append("严重等级：", style=_FIELD_STYLE)
        severity_color = get_severity_color(str(severity).lower())
        text.append(str(severity).upper(), style=f"bold {severity_color}")

    cvss = report.get("cvss")
    if cvss is not None:
        text.append("\n\n")
        text.append("CVSS 评分：", style=_FIELD_STYLE)
        try:
            score = float(cvss)
            text.append(f"{score:.1f}", style=f"bold {get_cvss_color(score)}")
        except (TypeError, ValueError):
            text.append(str(cvss))

    for key, label in (
        ("target", "目标"),
        ("endpoint", "端点"),
        ("method", "方法"),
        ("cve", "CVE"),
    ):
        value = report.get(key)
        if value:
            _append_field(text, label, str(value))

    vector = _cvss_vector(report)
    if vector:
        text.append("\n\n")
        text.append("CVSS 向量：", style=_FIELD_STYLE)
        text.append(vector, style="dim")

    dependency_metadata = report.get("dependency_metadata") or {}
    if isinstance(dependency_metadata, dict) and dependency_metadata:
        contextual_vector = dependency_metadata.get("contextual_cvss_vector")
        if contextual_vector:
            _append_field(text, "情境化 CVSS 向量", str(contextual_vector))

        advisory_cvss = dependency_metadata.get("advisory_cvss")
        if advisory_cvss is not None and advisory_cvss != report.get("cvss"):
            try:
                _append_field(text, "公告 CVSS", f"{float(advisory_cvss):.1f}")
            except (TypeError, ValueError):
                _append_field(text, "公告 CVSS", str(advisory_cvss))

        contextual_reasoning = dependency_metadata.get("contextual_cvss_reasoning")
        if contextual_reasoning:
            text.append("\n\n")
            text.append("情境化 CVSS 依据", style=_FIELD_STYLE)
            text.append(f"\n{contextual_reasoning}")

    for key, label in (
        ("description", "描述"),
        ("impact", "影响"),
        ("technical_analysis", "技术分析"),
        ("poc_description", "PoC 说明"),
    ):
        value = report.get(key)
        if value:
            text.append("\n\n")
            text.append(label, style=_FIELD_STYLE)
            text.append(f"\n{value}")

    poc_script_code = report.get("poc_script_code")
    if poc_script_code:
        text.append("\n\n")
        text.append("PoC 代码", style=_FIELD_STYLE)
        text.append(f"\n{poc_script_code}", style="dim")

    code_locations = report.get("code_locations")
    if isinstance(code_locations, list) and code_locations:
        text.append("\n\n")
        text.append("代码位置", style=_FIELD_STYLE)
        for i, loc in enumerate(code_locations):
            if not isinstance(loc, dict):
                continue
            text.append("\n\n")
            text.append(f"  位置 {i + 1}：", style="dim")
            text.append(str(loc.get("file", "unknown")), style="bold")
            start = loc.get("start_line")
            end = loc.get("end_line")
            if start is not None:
                if end and end != start:
                    text.append(f":{start}-{end}")
                else:
                    text.append(f":{start}")
            if loc.get("label"):
                text.append(f"\n  {loc['label']}", style="italic dim")
            if loc.get("snippet"):
                text.append("\n  ")
                text.append(str(loc["snippet"]), style="dim")
            if loc.get("fix_before") or loc.get("fix_after"):
                text.append("\n  修复：")
                if loc.get("fix_before"):
                    text.append("\n  - ", style="dim")
                    text.append(str(loc["fix_before"]), style="dim")
                if loc.get("fix_after"):
                    text.append("\n  + ", style="dim")
                    text.append(str(loc["fix_after"]), style="dim")

    remediation_steps = report.get("remediation_steps")
    if remediation_steps:
        text.append("\n\n")
        text.append("修复建议", style=_FIELD_STYLE)
        text.append(f"\n{remediation_steps}")

    return text


def _severity_counts(reports: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(_SEVERITY_ORDER, 0)
    for report in reports:
        if not isinstance(report, dict):
            continue
        severity = str(report.get("severity", "")).lower()
        if severity in counts:
            counts[severity] += 1
    return counts


def _append_severity_parts(stats_text: Text, counts: dict[str, int]) -> None:
    parts: list[Text] = []
    for severity in _SEVERITY_ORDER:
        count = counts[severity]
        if count <= 0:
            continue
        color = get_severity_color(severity)
        part = Text()
        part.append(f"{severity.upper()}: ", style=color)
        part.append(str(count), style=f"bold {color}")
        parts.append(part)
    for i, part in enumerate(parts):
        stats_text.append(part)
        if i < len(parts) - 1:
            stats_text.append(" | ", style="dim white")


def _build_vulnerability_stats(stats_text: Text, report_state: Any) -> None:
    reports = list(getattr(report_state, "vulnerability_reports", None) or [])
    vuln_count = len(reports)

    if vuln_count == 0:
        stats_text.append("漏洞  ", style="bold #22c55e")
        stats_text.append("0", style="bold white")
        stats_text.append("（未发现可利用漏洞）", style="dim green")
        stats_text.append("\n")
        return

    stats_text.append("漏洞  ", style="bold red")
    _append_severity_parts(stats_text, _severity_counts(reports))
    stats_text.append("（合计：", style="dim white")
    stats_text.append(str(vuln_count), style="bold yellow")
    stats_text.append("）", style="dim white")
    stats_text.append("\n")


def _llm_usage(report_state: Any) -> dict[str, Any]:
    getter = getattr(report_state, "get_total_llm_usage", None)
    if callable(getter):
        usage = getter()
        return usage if isinstance(usage, dict) else {}
    record = getattr(report_state, "run_record", None)
    usage = record.get("llm_usage") if isinstance(record, dict) else None
    return usage if isinstance(usage, dict) else {}


def is_subscription_run(report_state: Any) -> bool:
    """本次运行是否走订阅制（不计费）。

    优先读 run 记录，保证 hydrate/resume 的历史 run 也判断正确；
    记录里没有时回落到当前配置。
    """
    record = getattr(report_state, "run_record", None)
    if isinstance(record, dict) and record.get("auth_mode"):
        return record.get("auth_mode") == "subscription"

    from Ben.config import codex

    try:
        return codex.auth_mode(load_settings().llm.model) == "subscription"
    except Exception:  # noqa: BLE001 - 展示层不得因鉴权探测失败而中断
        return False


def _int_stat(usage: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(usage.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _float_stat(usage: dict[str, Any], key: str) -> float:
    try:
        value = float(usage.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _detail_value(usage: dict[str, Any], detail_key: str, value_key: str) -> int:
    details = usage.get(detail_key)
    if isinstance(details, list):
        details = details[0] if details and isinstance(details[0], dict) else {}
    if not isinstance(details, dict):
        return 0
    return _int_stat(details, value_key)


def has_model_response(report_state: Any) -> bool:
    """模型是否已经真实响应过（用于区分「启动中」与「已开始工作」）。"""
    usage = _llm_usage(report_state)
    return bool(usage) and _int_stat(usage, "requests") > 0


def _build_llm_usage_stats(
    stats_text: Text,
    report_state: Any,
    *,
    live: bool = False,
) -> None:
    subscription = is_subscription_run(report_state)
    usage = _llm_usage(report_state)

    if not usage or _int_stat(usage, "requests") <= 0:
        stats_text.append("\n")
        stats_text.append("成本 ", style="dim")
        if subscription:
            stats_text.append("$0.00 ", style="#22c55e")
            stats_text.append("（订阅） ", style="dim")
        else:
            stats_text.append("$0.0000 ", style="#fbbf24")
        stats_text.append("· ", style="dim white")
        stats_text.append("Token ", style="dim")
        stats_text.append("0", style="white")
        return

    input_tokens = _int_stat(usage, "input_tokens")
    output_tokens = _int_stat(usage, "output_tokens")
    cached_tokens = _detail_value(usage, "input_tokens_details", "cached_tokens")
    cost = _float_stat(usage, "cost")

    stats_text.append("\n")
    stats_text.append("输入 Token ", style="dim")
    stats_text.append(format_token_count(input_tokens), style="white")

    if live or cached_tokens > 0:
        stats_text.append("  ·  ", style="dim white")
        stats_text.append("缓存 Token ", style="dim")
        stats_text.append(format_token_count(cached_tokens), style="white")

    separator = "\n" if live else "  ·  "
    stats_text.append(separator, style="dim white")
    stats_text.append("输出 Token ", style="dim")
    stats_text.append(format_token_count(output_tokens), style="white")

    if subscription:
        stats_text.append("  ·  ", style="dim white")
        stats_text.append("成本 ", style="dim")
        stats_text.append("$0.00", style="#22c55e")
        stats_text.append("（订阅）", style="dim")
    elif live or cost > 0:
        stats_text.append("  ·  ", style="dim white")
        stats_text.append("成本 ", style="dim")
        stats_text.append(f"${cost:.4f}", style="#fbbf24")


def build_final_stats_text(report_state: Any) -> Text:
    stats_text = Text()
    if not report_state:
        return stats_text

    _build_vulnerability_stats(stats_text, report_state)
    _build_llm_usage_stats(stats_text, report_state)

    return stats_text


def build_live_stats_text(report_state: Any) -> Text:
    stats_text = Text()
    if not report_state:
        return stats_text

    model = load_settings().llm.model or "unknown"
    stats_text.append("模型 ", style="dim")
    stats_text.append(str(model), style="white")
    if is_subscription_run(report_state):
        stats_text.append("  ·  ", style="dim white")
        stats_text.append("ChatGPT 订阅", style="#22c55e")
    stats_text.append("\n")

    reports = list(getattr(report_state, "vulnerability_reports", None) or [])
    stats_text.append("漏洞 ", style="dim")
    stats_text.append(str(len(reports)), style="white")
    stats_text.append("\n")
    if reports:
        _append_severity_parts(stats_text, _severity_counts(reports))
        stats_text.append("\n")

    _build_llm_usage_stats(stats_text, report_state, live=True)

    return stats_text


def build_tui_stats_text(report_state: Any) -> Text:
    """TUI 顶栏用的精简版（单行优先）。"""
    stats_text = Text()
    if not report_state:
        return stats_text

    model = load_settings().llm.model or "unknown"
    stats_text.append(str(model), style="white")
    subscription = is_subscription_run(report_state)
    if subscription:
        stats_text.append("\n")
        stats_text.append("ChatGPT 订阅", style="#22c55e")

    usage = _llm_usage(report_state)
    if usage and _int_stat(usage, "total_tokens") > 0:
        stats_text.append("\n")
        stats_text.append(
            f"{format_token_count(_int_stat(usage, 'total_tokens'))} token",
            style="white",
        )
        cost = _float_stat(usage, "cost")
        if subscription:
            stats_text.append(" · ", style="white")
            stats_text.append("$0.00", style="white")
        elif cost > 0:
            stats_text.append(" · ", style="white")
            stats_text.append(f"${cost:.2f}", style="white")

    caido_url = getattr(report_state, "caido_url", None)
    if caido_url:
        stats_text.append("\n")
        stats_text.append("Caido: ", style="bold white")
        stats_text.append(str(caido_url), style="white")

    return stats_text
