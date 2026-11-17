"""意图图 + DFS pick（Sapper 同构，裁剪到 Ben 最小集）。

Fact 编码「见过什么」；Intent 编码「下一步干什么」并携带 profile。
ready = open ∧ from 全部存在；pick = 依赖链最深者优先，同深取新。
持久化 ``{state_dir}/task_graph.json``，模式对齐 coverage / claims。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from Ben.core.jsonstore import atomic_write_json

logger = logging.getLogger(__name__)

FACT_KINDS: frozenset[str] = frozenset(
    {"host", "service", "dir", "endpoint", "vuln", "cred", "note"}
)
INTENT_STATUSES: frozenset[str] = frozenset(
    {"open", "claimed", "done", "abandoned"}
)
# 首轮 seed 只派生 recon/enum，实现「初始阶段少挂载」。
_SEED_PROFILE_BY_KIND: dict[str, str] = {
    "host": "recon",
    "service": "recon",
    "dir": "enum",
    "endpoint": "enum",
}

_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "host": ("host",),
    "service": ("host", "port", "proto"),
    "dir": ("url",),
    "endpoint": ("method", "url"),
    "vuln": ("host", "port", "cve_or_name"),
    "cred": ("username", "host", "service"),
    "note": ("title",),
}

_TRACKING_QUERY_PREFIXES: tuple[str, ...] = ("utm_", "sid", "fbclid", "gclid")

_lock = threading.RLock()
_path: Path | None = None
_facts: dict[str, dict[str, Any]] = {}
_intents: dict[str, dict[str, Any]] = {}
_dedupe_index: dict[str, str] = {}  # hash → fact_id


def reset_task_graph() -> None:
    """测试用。"""
    global _path  # noqa: PLW0603
    with _lock:
        _facts.clear()
        _intents.clear()
        _dedupe_index.clear()
        _path = None


def hydrate_task_graph_from_disk(state_dir: Path) -> None:
    global _path  # noqa: PLW0603
    _path = state_dir / "task_graph.json"
    with _lock:
        _facts.clear()
        _intents.clear()
        _dedupe_index.clear()
        if not _path.exists():
            return
        try:
            data = json.loads(_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "task_graph.json at %s unreadable; starting empty", _path
            )
            return
        if not isinstance(data, dict):
            return
        facts = data.get("facts") or {}
        intents = data.get("intents") or {}
        dedupe = data.get("dedupe_index") or {}
        if isinstance(facts, dict):
            _facts.update(
                {
                    fid: dict(entry)
                    for fid, entry in facts.items()
                    if isinstance(fid, str) and isinstance(entry, dict)
                }
            )
        if isinstance(intents, dict):
            _intents.update(
                {
                    iid: dict(entry)
                    for iid, entry in intents.items()
                    if isinstance(iid, str) and isinstance(entry, dict)
                }
            )
        if isinstance(dedupe, dict):
            _dedupe_index.update(
                {
                    str(k): str(v)
                    for k, v in dedupe.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
            )
        logger.info(
            "task_graph hydrated from %s (%d fact(s), %d intent(s))",
            _path,
            len(_facts),
            len(_intents),
        )


def _persist_locked() -> None:
    path = _path
    if path is None:
        return
    try:
        atomic_write_json(
            path,
            {
                "facts": _facts,
                "intents": _intents,
                "dedupe_index": _dedupe_index,
            },
        )
    except OSError:
        logger.exception("task_graph persist failed at %s", path)


def canonical_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.lower()
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not any(k.lower().startswith(p) for p in _TRACKING_QUERY_PREFIXES)
    ]
    query_items.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(query_items, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def dedupe_key(kind: str, data: dict[str, Any], *, title: str = "") -> str:
    keys = _IDENTITY_KEYS.get(kind, ("title",))
    canon: dict[str, str] = {}
    for key in keys:
        if key == "title":
            canon[key] = (title or str(data.get("title") or "")).strip().lower()
        elif key == "url":
            canon[key] = canonical_url(str(data.get("url") or ""))
        else:
            canon[key] = str(data.get(key, "")).strip().lower()
    payload = f"{kind}|{json.dumps(canon, sort_keys=True, ensure_ascii=False)}"
    return hashlib.sha1(payload.encode()).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def add_fact(
    *,
    kind: str,
    title: str,
    data: dict[str, Any] | None = None,
    parent_fact: str | None = None,
    source_intent: str | None = None,
    creator: str | None = None,
    description: str = "",
    doc_path: str | None = None,
    derive_intent: bool = True,
) -> dict[str, Any]:
    """写入 fact；同 identity 命中则返回已有 id。可选派生首轮 intent。"""
    kind_s = (kind or "").strip().lower()
    if kind_s not in FACT_KINDS:
        return {"success": False, "error": f"unknown fact kind {kind!r}"}
    title_s = (title or "").strip()[:90]
    if not title_s:
        return {"success": False, "error": "title required"}
    data_d = dict(data or {})
    key = dedupe_key(kind_s, data_d, title=title_s)
    with _lock:
        existing = _dedupe_index.get(key)
        if existing and existing in _facts:
            return {
                "success": True,
                "fact_id": existing,
                "deduped": True,
                "fact": dict(_facts[existing]),
            }
        fact_id = _new_id("f")
        now = time.time()
        entry = {
            "id": fact_id,
            "kind": kind_s,
            "title": title_s,
            "description": (description or "").strip()[:240],
            "doc_path": doc_path,
            "data": data_d,
            "parent_fact": parent_fact,
            "source_intent": source_intent,
            "created_at": now,
            "creator": creator,
        }
        _facts[fact_id] = entry
        _dedupe_index[key] = fact_id
        intent_id = None
        if derive_intent:
            intent_id = _derive_intent_locked(fact_id, entry)
        _persist_locked()
        result = {
            "success": True,
            "fact_id": fact_id,
            "deduped": False,
            "fact": dict(entry),
        }
        if intent_id:
            result["intent_id"] = intent_id
        return result


def _derive_intent_locked(fact_id: str, fact: dict[str, Any]) -> str | None:
    profile = _SEED_PROFILE_BY_KIND.get(str(fact.get("kind") or ""))
    if profile is None:
        return None
    from_ids = [fact_id]
    dedupe = _intent_dedupe_key(profile, from_ids)
    for intent in _intents.values():
        if (
            intent.get("status") in {"open", "claimed"}
            and _intent_dedupe_key(
                str(intent.get("profile") or ""),
                list(intent.get("from") or []),
            )
            == dedupe
        ):
            return str(intent.get("id"))
    surface, risk = _surface_risk_for_fact(fact)
    intent_id = _new_id("i")
    title = f"{profile}: {fact.get('title')}"[:90]
    _intents[intent_id] = {
        "id": intent_id,
        "from": from_ids,
        "to": None,
        "profile": profile,
        "title": title,
        "description": fact.get("description") or title,
        "surface": surface,
        "risk_area": risk,
        "status": "open",
        "worker": None,
        "created_at": time.time(),
        "concluded_at": None,
    }
    return intent_id


def _intent_dedupe_key(profile: str, from_ids: list[str]) -> str:
    return f"{profile}|{','.join(sorted(from_ids))}"


def _surface_risk_for_fact(fact: dict[str, Any]) -> tuple[str, str]:
    kind = str(fact.get("kind") or "")
    data = fact.get("data") if isinstance(fact.get("data"), dict) else {}
    if kind == "service":
        host = data.get("host") or ""
        port = data.get("port") or ""
        return (f"{host}:{port}", "recon")
    if kind in {"dir", "endpoint"}:
        url = data.get("url") or fact.get("title") or ""
        return (str(url), "recon" if kind == "dir" else "enum")
    if kind == "host":
        return (str(data.get("host") or fact.get("title") or ""), "recon")
    return (str(fact.get("title") or fact.get("id") or "fact"), "recon")


def add_intent(
    *,
    profile: str,
    from_facts: list[str],
    title: str,
    description: str = "",
    surface: str | None = None,
    risk_area: str | None = None,
) -> dict[str, Any]:
    profile_s = (profile or "").strip().lower() or "recon"
    from_ids = [f for f in from_facts if f in _facts]
    if not from_ids:
        return {"success": False, "error": "from_facts must reference existing facts"}
    dedupe = _intent_dedupe_key(profile_s, from_ids)
    with _lock:
        for intent in _intents.values():
            if (
                intent.get("status") in {"open", "claimed"}
                and _intent_dedupe_key(
                    str(intent.get("profile") or ""),
                    list(intent.get("from") or []),
                )
                == dedupe
            ):
                return {
                    "success": True,
                    "intent_id": intent["id"],
                    "deduped": True,
                    "intent": dict(intent),
                }
        intent_id = _new_id("i")
        entry = {
            "id": intent_id,
            "from": from_ids,
            "to": None,
            "profile": profile_s,
            "title": (title or "").strip()[:90] or f"{profile_s} task",
            "description": (description or "").strip()[:400],
            "surface": (surface or "").strip(),
            "risk_area": (risk_area or "").strip() or profile_s,
            "status": "open",
            "worker": None,
            "created_at": time.time(),
            "concluded_at": None,
        }
        _intents[intent_id] = entry
        _persist_locked()
        return {
            "success": True,
            "intent_id": intent_id,
            "deduped": False,
            "intent": dict(entry),
        }


def _depth_locked(intent: dict[str, Any]) -> int:
    """依赖链深度：from 中 fact 沿 parent_fact 走到种子的最大层数。"""
    max_depth = 0
    for fid in intent.get("from") or []:
        depth = 0
        seen: set[str] = set()
        cur = fid
        while cur and cur not in seen:
            seen.add(cur)
            fact = _facts.get(cur)
            if not fact:
                break
            parent = fact.get("parent_fact")
            if not parent:
                break
            depth += 1
            cur = str(parent)
        max_depth = max(max_depth, depth)
    return max_depth


def _is_ready_locked(intent: dict[str, Any]) -> bool:
    if intent.get("status") != "open":
        return False
    return all(fid in _facts for fid in intent.get("from") or [])


def pick(worker: str) -> dict[str, Any] | None:
    """DFS pick：深度降序，同深按 created_at 降序；认领 intent → claimed。"""
    with _lock:
        ready = [i for i in _intents.values() if _is_ready_locked(i)]
        if not ready:
            return None
        ready.sort(
            key=lambda i: (-_depth_locked(i), -float(i.get("created_at") or 0.0))
        )
        chosen = ready[0]
        chosen["status"] = "claimed"
        chosen["worker"] = worker
        _persist_locked()
        return dict(chosen)


def peek_ready(limit: int = 8) -> list[dict[str, Any]]:
    """只读：按 DFS 序返回前 N 个 ready intent（不认领）。"""
    with _lock:
        ready = [dict(i) for i in _intents.values() if _is_ready_locked(i)]
        ready.sort(
            key=lambda i: (-_depth_locked(i), -float(i.get("created_at") or 0.0))
        )
        out: list[dict[str, Any]] = []
        for intent in ready[: max(0, limit)]:
            row = dict(intent)
            row["depth"] = _depth_locked(intent)
            out.append(row)
        return out


def get_intent(intent_id: str) -> dict[str, Any] | None:
    with _lock:
        entry = _intents.get(intent_id)
        return dict(entry) if entry else None


def get_fact(fact_id: str) -> dict[str, Any] | None:
    with _lock:
        entry = _facts.get(fact_id)
        return dict(entry) if entry else None


def list_facts(*, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        rows = list(_facts.values())
    if kind:
        kind_s = kind.strip().lower()
        rows = [r for r in rows if r.get("kind") == kind_s]
    rows.sort(key=lambda r: float(r.get("created_at") or 0.0))
    return [dict(r) for r in rows[: max(0, limit)]]


def conclude_intent(
    intent_id: str,
    *,
    to_fact: str | None = None,
    status: str = "done",
) -> dict[str, Any]:
    if status not in INTENT_STATUSES:
        return {"success": False, "error": f"bad status {status!r}"}
    with _lock:
        intent = _intents.get(intent_id)
        if intent is None:
            return {"success": False, "error": f"unknown intent {intent_id}"}
        intent["status"] = status
        intent["to"] = to_fact
        intent["concluded_at"] = time.time()
        _persist_locked()
        return {"success": True, "intent": dict(intent)}


def reopen_intent(intent_id: str) -> dict[str, Any]:
    """spawn 失败 / finish 未结论时把 claimed 放回 open。"""
    with _lock:
        intent = _intents.get(intent_id)
        if intent is None:
            return {"success": False, "error": f"unknown intent {intent_id}"}
        if intent.get("status") == "claimed":
            intent["status"] = "open"
            intent["worker"] = None
            _persist_locked()
        return {"success": True, "intent": dict(intent)}


def claim_intent(intent_id: str, worker: str) -> dict[str, Any]:
    with _lock:
        intent = _intents.get(intent_id)
        if intent is None:
            return {"success": False, "error": f"unknown intent {intent_id}"}
        if intent.get("status") != "open":
            return {
                "success": False,
                "error": f"intent {intent_id} status={intent.get('status')}",
                "intent": dict(intent),
            }
        # _is_ready_locked 要求 status==open；此处已确认 open，只校验 from。
        if not all(fid in _facts for fid in intent.get("from") or []):
            return {"success": False, "error": "intent dependencies not ready"}
        intent["status"] = "claimed"
        intent["worker"] = worker
        _persist_locked()
        return {"success": True, "intent": dict(intent)}


def select_next_intent(
    *,
    profiles: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """只读选出 DFS 队首（不认领），供 create_agent 强制代领。"""
    with _lock:
        ready = [i for i in _intents.values() if _is_ready_locked(i)]
        if profiles is not None:
            ready = [
                i for i in ready if str(i.get("profile") or "") in profiles
            ]
        if not ready:
            return None
        ready.sort(
            key=lambda i: (-_depth_locked(i), -float(i.get("created_at") or 0.0))
        )
        chosen = ready[0]
        row = dict(chosen)
        row["depth"] = _depth_locked(chosen)
        return row


def has_open_intents(*, profiles: frozenset[str] | None = None) -> bool:
    with _lock:
        for intent in _intents.values():
            if not _is_ready_locked(intent):
                continue
            if profiles is not None and str(intent.get("profile") or "") not in profiles:
                continue
            return True
        return False


def release_intents_for_agent(
    agent_id: str,
    *,
    intent_ids: list[str] | None = None,
) -> list[str]:
    """终态 reaper：把仍 claimed 且 worker=agent_id 的 intent 放回 open。"""
    agent_s = (agent_id or "").strip()
    if not agent_s:
        return []
    reopened: list[str] = []
    with _lock:
        if intent_ids is not None:
            candidates = [
                _intents[iid]
                for iid in intent_ids
                if iid in _intents
            ]
        else:
            candidates = [
                i
                for i in _intents.values()
                if i.get("status") == "claimed" and i.get("worker") == agent_s
            ]
        for intent in candidates:
            if intent.get("status") != "claimed":
                continue
            if intent.get("worker") != agent_s:
                continue
            intent["status"] = "open"
            intent["worker"] = None
            reopened.append(str(intent.get("id")))
        if reopened:
            _persist_locked()
    return reopened


def conclude_intents(
    intent_ids: list[str],
    *,
    status: str = "done",
) -> list[str]:
    """批量 conclude（合批 finish）。"""
    done: list[str] = []
    for iid in intent_ids:
        result = conclude_intent(iid, status=status)
        if result.get("success"):
            done.append(iid)
    return done


def sweep_stale_claimed_intents(
    *,
    live_agent_ids: set[str] | frozenset[str] | None = None,
    terminal_agent_ids: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """resume/启动清扫：worker 已终态或不在 live 集合的 claimed → open。"""
    reopened: list[str] = []
    live = set(live_agent_ids or ())
    terminal = set(terminal_agent_ids or ())
    with _lock:
        for intent in _intents.values():
            if intent.get("status") != "claimed":
                continue
            worker = str(intent.get("worker") or "").strip()
            if not worker:
                intent["status"] = "open"
                intent["worker"] = None
                reopened.append(str(intent.get("id")))
                continue
            stale = (terminal and worker in terminal) or (
                live_agent_ids is not None and worker not in live
            )
            if stale:
                intent["status"] = "open"
                intent["worker"] = None
                reopened.append(str(intent.get("id")))
        if reopened:
            _persist_locked()
    return reopened


def claim_sibling_batch(
    seed_intent_id: str,
    worker: str,
    *,
    limit: int = 5,
) -> list[str]:
    """认领主 intent 的同父同 profile 兄弟（含自身若仍 open）。返回全部 claimed id。"""
    siblings = batch_sibling_intents(seed_intent_id, limit=limit)
    ids = [str(s.get("id")) for s in siblings if s.get("id")]
    if seed_intent_id not in ids:
        ids.insert(0, seed_intent_id)
    claimed_ids: list[str] = []
    with _lock:
        for iid in ids:
            intent = _intents.get(iid)
            if intent is None:
                continue
            if intent.get("status") == "claimed" and intent.get("worker") == worker:
                claimed_ids.append(iid)
                continue
            if intent.get("status") != "open":
                continue
            if not all(fid in _facts for fid in intent.get("from") or []):
                continue
            intent["status"] = "claimed"
            intent["worker"] = worker
            claimed_ids.append(iid)
        if claimed_ids:
            _persist_locked()
    return claimed_ids


def ingest_seed_facts(
    facts: list[dict[str, Any]],
    *,
    creator: str = "seed",
) -> dict[str, Any]:
    """把 seed/scout 产物落成 depth-0 fact，并只派生 recon/enum intent。"""
    added: list[str] = []
    deduped: list[str] = []
    intents: list[str] = []
    for raw in facts:
        if not isinstance(raw, dict):
            continue
        result = add_fact(
            kind=str(raw.get("kind") or ""),
            title=str(raw.get("title") or ""),
            data=raw.get("data") if isinstance(raw.get("data"), dict) else {},
            creator=creator,
            description=str(raw.get("description") or ""),
            derive_intent=True,
        )
        if not result.get("success"):
            continue
        fid = str(result["fact_id"])
        if result.get("deduped"):
            deduped.append(fid)
        else:
            added.append(fid)
        if result.get("intent_id"):
            intents.append(str(result["intent_id"]))
    return {
        "success": True,
        "added": added,
        "deduped": deduped,
        "intents": intents,
    }


def snapshot_counts() -> dict[str, int]:
    with _lock:
        open_n = sum(1 for i in _intents.values() if i.get("status") == "open")
        claimed_n = sum(1 for i in _intents.values() if i.get("status") == "claimed")
        return {
            "facts": len(_facts),
            "intents": len(_intents),
            "open": open_n,
            "claimed": claimed_n,
        }


def batch_sibling_intents(
    seed_intent_id: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """同父、同 profile 的 open 兄弟 intent（含自身），供 ephemeral 合批。"""
    with _lock:
        seed = _intents.get(seed_intent_id)
        if seed is None:
            return []
        profile = seed.get("profile")
        parents = frozenset(seed.get("from") or [])
        siblings = [
            dict(i)
            for i in _intents.values()
            if i.get("status") == "open"
            and i.get("profile") == profile
            and frozenset(i.get("from") or []) == parents
        ]
        # 确保 seed 在列（可能已 claimed）
        if seed.get("status") == "claimed":
            siblings = [dict(seed), *siblings]
        siblings.sort(key=lambda i: -float(i.get("created_at") or 0.0))
        return siblings[: max(1, limit)]
