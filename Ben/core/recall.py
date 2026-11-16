"""Spawn 前保守 recall：coverage/notes 的 token-overlap top-K。

无 embedding；CJK 按字切分；重叠率 ≥0.34 才收录；拼进 task ≤800 字。
"""

from __future__ import annotations

import logging
import re
from typing import Any


logger = logging.getLogger(__name__)

_RECALL_MIN_SCORE = 0.34
_RECALL_TOP_K = 3
_RECALL_MAX_CHARS = 800
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", re.UNICODE)


def tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _TOKEN_RE.finditer(text or ""):
        tok = match.group(0).lower()
        if tok:
            tokens.add(tok)
    return tokens


def overlap_score(query: set[str], doc: set[str]) -> float:
    if not query or not doc:
        return 0.0
    inter = len(query & doc)
    if inter == 0:
        return 0.0
    return inter / min(len(query), len(doc))


def _coverage_docs() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    try:
        from Ben.tools.coverage.tools import get_coverage_entries

        for entry in get_coverage_entries():
            surface = str(entry.get("surface") or "")
            risk = str(entry.get("risk_area") or "")
            outcome = str(entry.get("outcome") or "")
            evidence = str(entry.get("evidence") or "")
            title = f"{surface} · {risk} ({outcome})"
            body = f"{surface} {risk} {outcome} {evidence}".strip()
            docs.append({"kind": "coverage", "title": title, "body": body})
    except Exception:  # noqa: BLE001
        logger.debug("recall: coverage docs failed", exc_info=True)
    return docs


def _notes_docs() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    try:
        from Ben.tools.notes import tools as notes_tools

        storage = getattr(notes_tools, "_notes_storage", {})
        if not isinstance(storage, dict):
            return docs
        for note in storage.values():
            if not isinstance(note, dict):
                continue
            title = str(note.get("title") or "")
            content = str(note.get("content") or "")
            docs.append(
                {
                    "kind": "note",
                    "title": title or content[:60],
                    "body": f"{title} {content}".strip(),
                }
            )
    except Exception:  # noqa: BLE001
        logger.debug("recall: notes docs failed", exc_info=True)
    return docs


def recall_similar(task: str, *, top_k: int = _RECALL_TOP_K) -> list[dict[str, Any]]:
    query = tokenize(task)
    if not query:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for doc in (*_coverage_docs(), *_notes_docs()):
        score = overlap_score(query, tokenize(str(doc.get("body") or "")))
        if score >= _RECALL_MIN_SCORE:
            scored.append((score, doc))
    scored.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, Any]] = []
    for score, doc in scored[: max(1, top_k)]:
        out.append({**doc, "score": round(score, 3)})
    return out


def format_recall_block(task: str, *, max_chars: int = _RECALL_MAX_CHARS) -> str:
    hits = recall_similar(task)
    if not hits:
        return ""
    lines = ["已知/已排除（自动召回，供参考）："]
    for hit in hits:
        kind = hit.get("kind", "?")
        title = str(hit.get("title") or "").strip()
        lines.append(f"- [{kind}] {title}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def augment_task_with_recall(task: str) -> str:
    block = format_recall_block(task)
    if not block:
        return task
    if "已知/已排除" in task:
        return task
    return f"{task.rstrip()}\n\n{block}"
