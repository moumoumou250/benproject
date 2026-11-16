"""进 prompt 的账本统一截断口径（索引层，不是正文）。

对齐 sapper 的 render 纪律：默认只给短行摘要，详情经工具按需拉取。
"""

from __future__ import annotations

from typing import Any

# 单行索引上限（facts / notes preview / graph 旁注共用）
INDEX_LINE_MAX = 90
# 注入子 agent 的共享发现条数上限
INDEX_MAX_ITEMS = 30
# list_notes 默认 preview（原 280，索引层收紧）
LIST_PREVIEW_CHARS = 90


def clip_line(text: str, limit: int = INDEX_LINE_MAX) -> str:
    """单行截断：去换行，超长加省略号。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[: max(1, limit - 3)].rstrip()
    space = cut.rfind(" ")
    if space >= max(16, (limit - 3) // 2):
        cut = cut[:space].rstrip()
    return cut + "..."


def build_shared_findings_index(*, max_items: int = INDEX_MAX_ITEMS) -> str:
    """从报告台账 + notes 拼一份短索引（权威在报告，notes 作补充）。

    无状态或为空时返回空串；调用方决定是否写入子 agent 首条消息。
    """
    lines: list[str] = []
    reports: list[dict[str, Any]] = []

    try:
        from Ben.report.state import get_global_report_state

        state = get_global_report_state()
    except Exception:  # noqa: BLE001 - 索引失败不应阻断派生
        state = None
    if state is not None:
        try:
            reports = list(state.get_existing_vulnerabilities() or [])
        except Exception:  # noqa: BLE001
            reports = list(getattr(state, "vulnerability_reports", None) or [])
        for report in reports:
            if len(lines) >= max_items:
                break
            rid = str(report.get("id") or report.get("report_id") or "?")
            sev = str(report.get("severity") or "?").lower()
            title = clip_line(str(report.get("title") or "(untitled)"))
            endpoint = report.get("endpoint") or report.get("target") or ""
            suffix = f" @{clip_line(str(endpoint), 40)}" if endpoint else ""
            lines.append(f"vuln {rid} [{sev}] {title}{suffix}")

    try:
        from Ben.tools.notes import tools as notes_mod

        with notes_mod._notes_lock:
            notes = [dict(n) for n in notes_mod._notes_storage.values()]
    except Exception:  # noqa: BLE001
        notes = []

    # findings 优先，其余 category 垫后；同一 title 不重复刷屏。
    def _note_key(note: dict[str, Any]) -> tuple[int, str]:
        cat = str(note.get("category") or "general")
        pri = 0 if cat == "findings" else 1 if cat == "methodology" else 2
        return (pri, str(note.get("updated_at") or note.get("created_at") or ""))

    seen_titles = {clip_line(str(r.get("title") or "")) for r in reports}
    for note in sorted(notes, key=_note_key):
        if len(lines) >= max_items:
            break
        title = clip_line(str(note.get("title") or ""))
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        cat = str(note.get("category") or "general")
        nid = str(note.get("note_id") or "?")
        preview = clip_line(str(note.get("content") or ""), 60)
        extra = f" — {preview}" if preview else ""
        lines.append(f"note:{cat} #{nid} {title}{extra}")

    return "\n".join(lines)
