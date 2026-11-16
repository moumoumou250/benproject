"""Root 侧紧凑状态块：resume 已测面 + 每轮 frontier（Sapper hook 同构）。

两处共用「coverage → 短行」渲染，避免 root 反复调 list_coverage / 重测旧面。
注入策略见 :func:`inject_or_replace_frontier`：仅当次 ``input_items`` 内替换，
签名不变则跳过，防止会话历史每轮叠 6KB。
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from Ben.core.agents import ACTIVE_STATUSES, TERMINAL_STATUSES
from Ben.core.prompt_budget import clip_line


if TYPE_CHECKING:
    from Ben.core.agents import AgentCoordinator


logger = logging.getLogger(__name__)

EXCLUDED_OUTCOMES: frozenset[str] = frozenset(
    {"ruled_out", "no_issue_found", "not_applicable"}
)

FRONTIER_MARKER = "[Frontier status]"
MAX_FRONTIER_CHARS = 6_000
MAX_EXCLUDED_CHARS = 3_000
_MAX_EXCLUDED_ROWS = 40
_MAX_READY_ROWS = 8
_MAX_CLAIMED_ROWS = 8
_MAX_TODO_ROWS = 8
_MAX_AGENT_ROWS = 8

# risk_area → 建议 skill / tier / minutes（静态小表；未知走 hunter/15）
_RISK_SUGGEST: dict[str, tuple[str, str, int]] = {
    "xss": ("xss", "hunter", 25),
    "sqli": ("sql_injection", "hunter", 25),
    "sql_injection": ("sql_injection", "hunter", 25),
    "ssrf": ("ssrf", "hunter", 25),
    "auth": ("authentication", "hunter", 30),
    "authz": ("broken_access_control", "hunter", 30),
    "idor": ("broken_access_control", "hunter", 25),
    "recon": ("reconnaissance", "scout", 10),
    "validation": ("", "validator", 10),
}

# 按 agent_id 记住上次注入的 frontier 签名，签名不变则跳过。
_last_frontier_sig: dict[str, str] = {}


def clear_frontier_signatures() -> None:
    """测试用：清空节流签名。"""
    _last_frontier_sig.clear()


def render_excluded_coverage_brief(
    entries: list[dict[str, Any]] | None = None,
    *,
    max_chars: int = MAX_EXCLUDED_CHARS,
) -> str:
    """Resume 用：已排除面列表 +「新证据可推翻」措辞。无条目时返回空串。"""
    if entries is None:
        from Ben.tools.coverage.tools import get_coverage_entries

        entries = get_coverage_entries()

    rows: list[str] = []
    for entry in entries:
        outcome = str(entry.get("outcome") or "").strip().lower()
        if outcome not in EXCLUDED_OUTCOMES:
            continue
        surface = clip_line(str(entry.get("surface") or "?"), 50)
        risk = clip_line(str(entry.get("risk_area") or "?"), 40)
        rows.append(f"- [{outcome}] {surface} · {risk}")
        if len(rows) >= _MAX_EXCLUDED_ROWS:
            break

    if not rows:
        return ""

    header = (
        "## Previously excluded surfaces (resume)\n"
        "The following coverage rows were closed in a prior segment of this run "
        f"as {', '.join(sorted(EXCLUDED_OUTCOMES))}. Do **not** retest them by "
        "default. New evidence may reopen a row — call `update_coverage` (or "
        "record a fresh entry) before spending turns rediscovering the same "
        "surface/risk_area."
    )
    body = "\n".join(rows)
    omitted = sum(
        1
        for e in entries
        if str(e.get("outcome") or "").strip().lower() in EXCLUDED_OUTCOMES
    ) - len(rows)
    if omitted > 0:
        body += f"\n- … {omitted} more excluded row(s); call list_coverage for the rest"

    text = f"{header}\n{body}"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def merge_resume_instruction(
    user_instruction: str | None,
    *,
    excluded_brief: str | None = None,
) -> str:
    """把已测面 brief 与用户 resume instruction 合并；任一方非空即返回可发送文本。"""
    brief = excluded_brief if excluded_brief is not None else render_excluded_coverage_brief()
    user = (user_instruction or "").strip()
    if brief and user:
        return f"{brief}\n\n{user}"
    return brief or user


def _open_todos() -> list[dict[str, Any]]:
    try:
        from Ben.tools.todo import tools as todo_mod

        with todo_mod._todos_io_lock:
            flat: list[dict[str, Any]] = []
            for by_id in todo_mod._todos_storage.values():
                for todo in by_id.values():
                    status = str(todo.get("status") or "pending")
                    if status in {"pending", "in_progress"}:
                        flat.append(dict(todo))
            flat.sort(key=todo_mod._todo_sort_key)
            return flat
    except Exception:  # noqa: BLE001 - frontier 失败不应阻断 LLM 调用
        logger.debug("frontier: todo snapshot failed", exc_info=True)
        return []


def _coverage_gaps(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for entry in entries:
        if str(entry.get("outcome") or "") != "needs_follow_up":
            continue
        gaps.append(
            {
                "surface": entry.get("surface", ""),
                "risk_area": entry.get("risk_area", ""),
                "detail": entry.get("evidence") or "needs follow-up",
            }
        )
    return gaps


def _agent_lines(coordinator: AgentCoordinator | None) -> list[str]:
    if coordinator is None:
        return []
    lines: list[str] = []
    try:
        items = [
            (aid, coordinator.statuses.get(aid), coordinator.names.get(aid, aid))
            for aid, parent in coordinator.parent_of.items()
            if parent is not None
        ]
    except Exception:  # noqa: BLE001
        return []
    # 在飞优先，其次刚终结的
    active = [(a, s, n) for a, s, n in items if s in ACTIVE_STATUSES]
    terminal = [(a, s, n) for a, s, n in items if s in TERMINAL_STATUSES]
    for aid, status, name in (*active, *terminal)[:_MAX_AGENT_ROWS]:
        lines.append(f"- agent {aid} ({clip_line(str(name), 40)}): {status}")
    return lines


def _suggest_line(surface: str, risk: str) -> str:
    # L4：json.dumps，避免 surface 含引号时 Python repr 破形。
    import json

    key = risk.strip().lower()
    skill, tier, minutes = _RISK_SUGGEST.get(key, ("", "hunter", 15))
    name = f"{risk or 'task'} @ {surface}"[:40]
    skill_list = [skill] if skill else []
    return (
        "  suggest: "
        f"name={json.dumps(name, ensure_ascii=False)} "
        f"skills={json.dumps(skill_list, ensure_ascii=False)} "
        f"tier={json.dumps(tier, ensure_ascii=False)} minutes={minutes} "
        f"surface={json.dumps(surface, ensure_ascii=False)} "
        f"risk_area={json.dumps(risk, ensure_ascii=False)}"
    )


def _claimed_keys() -> dict[tuple[str, str], dict[str, Any]]:
    try:
        from Ben.core.task_claims import list_claims, normalize_claim_key

        out: dict[tuple[str, str], dict[str, Any]] = {}
        for claim in list_claims():
            key = normalize_claim_key(
                str(claim.get("surface") or ""),
                str(claim.get("risk_area") or ""),
            )
            if key[0] and key[1]:
                out[key] = claim
        return out
    except Exception:  # noqa: BLE001
        logger.debug("frontier: list_claims failed", exc_info=True)
        return {}


def _clip_section(lines: list[str], budget: int, *, more_hint: str) -> list[str]:
    """按字节预算折叠段尾，关键段（todo/agents）永不整段丢弃。"""
    if budget <= 0:
        return [lines[0], f"- … folded; {more_hint}"] if lines else []
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        add = len(line) + (1 if out else 0)
        if used + add > budget and i > 0:
            remaining = len(lines) - i
            out.append(f"- … {remaining} more; {more_hint}")
            break
        out.append(line)
        used += add
    return out


def render_frontier_block(
    *,
    coordinator: AgentCoordinator | None = None,
    entries: list[dict[str, Any]] | None = None,
    max_chars: int = MAX_FRONTIER_CHARS,
) -> str:
    """≤6KB 的 root 编排状态块（Ready / Claimed / todo / agents）。

    M4：不再重复渲染 Coverage gaps（Ready 已覆盖）；分段预算，不整块尾部截断。
    """
    if entries is None:
        try:
            from Ben.tools.coverage.tools import get_coverage_entries

            entries = get_coverage_entries()
        except Exception:  # noqa: BLE001
            entries = []

    header = [
        FRONTIER_MARKER,
        "Orchestration snapshot only — do not probe the target yourself; "
        "delegate via create_agent / send_message_to_agent / stop_agent.",
    ]

    claims = _claimed_keys()
    gaps = _coverage_gaps(entries)
    ready: list[dict[str, Any]] = []
    for gap in gaps:
        key = (
            str(gap.get("surface") or "").strip().lower(),
            str(gap.get("risk_area") or "").strip().lower(),
        )
        if key in claims:
            continue
        ready.append(gap)

    ready_lines = ["## Ready (unclaimed needs_follow_up)"]
    if ready:
        for gap in ready[:_MAX_READY_ROWS]:
            surface = clip_line(str(gap.get("surface") or "?"), 40)
            risk = clip_line(str(gap.get("risk_area") or "?"), 30)
            ready_lines.append(f"- {surface} · {risk}")
            ready_lines.append(
                _suggest_line(str(gap.get("surface") or ""), str(gap.get("risk_area") or ""))
            )
        omitted = len(ready) - _MAX_READY_ROWS
        if omitted > 0:
            ready_lines.append(f"- … {omitted} more Ready; call list_coverage")
    else:
        ready_lines.append("- (none)")

    claimed_lines = ["## Claimed"]
    if claims:
        shown = 0
        for claim in list(claims.values())[:_MAX_CLAIMED_ROWS]:
            surface = clip_line(str(claim.get("surface") or "?"), 40)
            risk = clip_line(str(claim.get("risk_area") or "?"), 30)
            owner = claim.get("agent_name") or claim.get("agent_id") or "?"
            elapsed = ""
            epoch = claim.get("claimed_at_epoch")
            if isinstance(epoch, (int, float)):
                import time as _time

                mins = max(0.0, (_time.time() - float(epoch)) / 60.0)
                elapsed = f", elapsed={mins:.1f}m"
            claimed_lines.append(f"- {surface} · {risk} → {owner}{elapsed}")
            shown += 1
        omitted = len(claims) - shown
        if omitted > 0:
            claimed_lines.append(f"- … {omitted} more claims")
    else:
        claimed_lines.append("- (none)")

    excluded_n = sum(
        1 for e in entries if str(e.get("outcome") or "").lower() in EXCLUDED_OUTCOMES
    )
    open_n = sum(1 for e in entries if str(e.get("outcome") or "") == "needs_follow_up")
    reported_n = sum(1 for e in entries if str(e.get("outcome") or "") == "reported")
    # M4：gaps 只保留计数，避免与 Ready 重复。
    counts_lines = [
        f"## Counts: total={len(entries)} open={open_n} "
        f"excluded={excluded_n} reported={reported_n} "
        f"ready={len(ready)} claimed={len(claims)} gaps_total={len(gaps)}"
    ]

    todos = _open_todos()
    todo_lines = ["## Open todos"]
    if todos:
        for todo in todos[:_MAX_TODO_ROWS]:
            status = todo.get("status", "pending")
            pri = todo.get("priority", "normal")
            title = clip_line(str(todo.get("title") or todo.get("content") or "?"), 60)
            todo_lines.append(f"- [{status}/{pri}] {title}")
        omitted = len(todos) - _MAX_TODO_ROWS
        if omitted > 0:
            todo_lines.append(f"- … {omitted} more; call list_todos")
    else:
        todo_lines.append("- (none)")

    agent_body = _agent_lines(coordinator)
    agent_lines = ["## Sub-agents"]
    if agent_body:
        agent_lines.extend(agent_body)
    else:
        agent_lines.append("- (none)")

    budgets = {"ready": 2400, "claimed": 900, "todo": 1200, "agents": 1200}
    sections = [
        _clip_section(ready_lines, budgets["ready"], more_hint="call list_coverage"),
        _clip_section(claimed_lines, budgets["claimed"], more_hint="call list_claims"),
        counts_lines,
        _clip_section(todo_lines, budgets["todo"], more_hint="call list_todos"),
        _clip_section(agent_lines, budgets["agents"], more_hint="call view_agent_graph"),
    ]
    text = "\n".join(header + [line for sec in sections for line in sec])
    if len(text) <= max_chars:
        return text
    # 最后兜底仍保关键关键段标题可见。
    return text[: max_chars - 1].rstrip() + "…"


def frontier_content_signature(block: str) -> str:
    """短签名：用于节流（内容未变则不重复注入）。"""
    # 去掉纯空白差异
    normalized = "\n".join(line.rstrip() for line in block.splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _item_text(item: Any) -> str:
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts)
    return ""


def inject_or_replace_frontier(
    input_items: list[Any],
    block: str,
    *,
    agent_id: str,
) -> bool:
    """在 ``input_items`` 内替换或追加 frontier 块。

    返回是否写入了新内容。签名与上次相同则跳过（即使用户历史里已有旧块也不再叠）。
    """
    if not block.strip() or FRONTIER_MARKER not in block:
        return False
    sig = frontier_content_signature(block)
    if _last_frontier_sig.get(agent_id) == sig:
        return False

    payload = {"role": "user", "content": block}
    for index, item in enumerate(input_items):
        if FRONTIER_MARKER in _item_text(item):
            input_items[index] = payload
            _last_frontier_sig[agent_id] = sig
            return True
    input_items.append(payload)
    _last_frontier_sig[agent_id] = sig
    return True
