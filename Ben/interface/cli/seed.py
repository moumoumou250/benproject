"""把 recon/scout 产物幂等注入扫描（Sapper 式种子，消除「侦察死胡同」）。

``ben recon`` / ``ben scout`` 写出 ``recon.json`` / ``scout.json`` / ``facts.jsonl``，
但扫描路径原先不消费它们。本模块在 ``prepare_run`` 里：

1. 解析 ``--seed`` 路径，或按目标 host 在 ``recon/`` 下自动选最新产物；
2. 去重（同 ``kind`` + 规范化 ``data`` 跳过）；
3. 渲染紧凑 brief 拼进 instruction，并把完整 facts 写入
   ``/workspace/preseed/facts.json``（content 形态，供 CLI 沙箱挂载）。

授权仍只由白名单闸门管网络；种子只是上下文，不扩大授权面。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_BRIEF_FACT_CAP = 24
_TITLE_CAP = 90
_JSON_NAMES = ("scout.json", "recon.json")


@dataclass(slots=True)
class SeedFact:
    kind: str
    title: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""


@dataclass(slots=True)
class SeedBundle:
    source_path: Path
    kind: str  # scout | recon | facts
    target: str = ""
    host: str = ""
    verdict: dict[str, Any] | None = None
    facts: list[SeedFact] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": str(self.source_path),
            "kind": self.kind,
            "target": self.target,
            "host": self.host,
            "verdict": self.verdict,
            "facts": [
                {
                    "kind": f.kind,
                    "title": f.title,
                    "data": f.data,
                    "source": f.source,
                }
                for f in self.facts
            ],
            "suggestions": self.suggestions,
            "notes": self.notes,
        }


def _slug(host: str) -> str:
    text = (host or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "_", text)
    return text.strip("._-") or "target"


def _host_from_target(target: str) -> str:
    text = (target or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


def hosts_from_targets_info(targets_info: list[dict[str, Any]] | None) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()
    for info in targets_info or []:
        details = info.get("details") or {}
        candidates = [
            details.get("target_url"),
            details.get("target_ip"),
            info.get("original"),
        ]
        for raw in candidates:
            host = _host_from_target(str(raw or ""))
            if host and host not in seen:
                seen.add(host)
                hosts.append(host)
    return hosts


def _fact_key(kind: str, title: str, data: dict[str, Any]) -> str:
    try:
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(data)
    return f"{kind}\0{title}\0{payload}"


def _dedupe_facts(facts: list[SeedFact]) -> list[SeedFact]:
    out: list[SeedFact] = []
    seen: set[str] = set()
    for fact in facts:
        key = _fact_key(fact.kind, fact.title, fact.data)
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


def _parse_fact_dict(raw: dict[str, Any]) -> SeedFact | None:
    kind = str(raw.get("kind") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not kind or not title:
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}
    return SeedFact(
        kind=kind,
        title=title,
        data=data,
        source=str(raw.get("source") or ""),
    )


def _load_json_document(path: Path) -> SeedBundle:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"seed JSON root must be an object: {path}")

    facts_raw = payload.get("facts")
    facts: list[SeedFact] = []
    if isinstance(facts_raw, list):
        for item in facts_raw:
            if isinstance(item, dict):
                parsed = _parse_fact_dict(item)
                if parsed is not None:
                    facts.append(parsed)

    verdict = payload.get("verdict")
    if not isinstance(verdict, dict):
        verdict = None

    suggestions = payload.get("suggestions") or []
    if not isinstance(suggestions, list):
        suggestions = []
    notes_raw = payload.get("notes") or []
    notes = [str(n) for n in notes_raw] if isinstance(notes_raw, list) else []

    kind = "scout" if path.name == "scout.json" or "scout" in path.parent.name else "recon"
    return SeedBundle(
        source_path=path,
        kind=kind,
        target=str(payload.get("target") or ""),
        host=str(payload.get("host") or ""),
        verdict=verdict,
        facts=_dedupe_facts(facts),
        suggestions=[s for s in suggestions if isinstance(s, dict)],
        notes=notes,
    )


def _load_facts_jsonl(path: Path) -> SeedBundle:
    facts: list[SeedFact] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(raw, dict):
            continue
        parsed = _parse_fact_dict(raw)
        if parsed is not None:
            facts.append(parsed)
    return SeedBundle(
        source_path=path,
        kind="facts",
        facts=_dedupe_facts(facts),
    )


def load_seed_path(path: Path) -> SeedBundle:
    """从文件或目录加载种子；目录优先 scout.json → recon.json → facts.jsonl。"""
    path = path.expanduser().resolve()
    if path.is_dir():
        for name in (*_JSON_NAMES, "facts.jsonl"):
            candidate = path / name
            if candidate.is_file():
                return load_seed_path(candidate)
        raise ValueError(
            f"seed directory {path} has no scout.json / recon.json / facts.jsonl"
        )
    if not path.is_file():
        raise ValueError(f"seed path is not a file: {path}")

    if path.name.endswith(".jsonl") or path.name == "facts.jsonl":
        return _load_facts_jsonl(path)
    if path.suffix.lower() == ".json":
        return _load_json_document(path)
    raise ValueError(f"unsupported seed file (want .json / .jsonl): {path}")


def find_auto_seed(
    hosts: list[str],
    *,
    recon_root: Path | None = None,
) -> Path | None:
    """在 ``recon/<slug>_*`` 下找最新匹配产物目录里的 json。

    同 host 优先 ``*_scout``，再普通 recon；按目录 mtime 取最新。
    """
    root = (recon_root or Path("recon")).expanduser()
    if not root.is_dir() or not hosts:
        return None

    best: tuple[float, int, Path] | None = None  # mtime, scout_bias, file
    for host in hosts:
        slug = _slug(host)
        prefix = f"{slug}_"
        try:
            children = list(root.iterdir())
        except OSError:
            return None
        for child in children:
            if not child.is_dir() or not child.name.startswith(prefix):
                continue
            scout_bias = 1 if child.name.endswith("_scout") else 0
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            for name in _JSON_NAMES:
                candidate = child / name
                if not candidate.is_file():
                    continue
                rank = (mtime, scout_bias, candidate)
                if best is None or rank[:2] > best[:2]:
                    best = rank
                break
    return best[2] if best else None


def render_seed_brief(bundle: SeedBundle) -> str:
    """紧凑状态块：标题行 + 短摘要，供拼进 root instruction。"""
    lines: list[str] = [
        "## Pre-seeded reconnaissance",
        (
            f"Source: `{bundle.source_path}` ({bundle.kind}). "
            "Treat these as already observed facts — do not spend early turns "
            "rediscovering host/service/surface structure. Verify, refine, and "
            "spawn specialists against them. Full structured facts are also at "
            "`/workspace/preseed/facts.json` when the sandbox mount succeeded."
        ),
    ]
    if bundle.host or bundle.target:
        lines.append(f"- target: {bundle.target or bundle.host}")
    if bundle.verdict:
        difficulty = bundle.verdict.get("difficulty") or bundle.verdict.get("verdict")
        why = bundle.verdict.get("why") or bundle.verdict.get("reason") or ""
        if difficulty:
            lines.append(f"- scout verdict: **{difficulty}**" + (f" — {why}" if why else ""))

    priority_kinds = ("host", "service", "http", "tls", "surface", "endpoint", "param", "note")
    ranked = sorted(
        bundle.facts,
        key=lambda f: (
            priority_kinds.index(f.kind) if f.kind in priority_kinds else len(priority_kinds),
            f.title,
        ),
    )
    for fact in ranked[:_BRIEF_FACT_CAP]:
        detail = _short_data(fact.data)
        title = fact.title if len(fact.title) <= _TITLE_CAP else fact.title[: _TITLE_CAP - 1] + "…"
        lines.append(f"- [{fact.kind}] {title}" + (f" — {detail}" if detail else ""))
    omitted = len(ranked) - _BRIEF_FACT_CAP
    if omitted > 0:
        lines.append(f"- … {omitted} more fact(s) in `/workspace/preseed/facts.json`")

    for suggestion in bundle.suggestions[:5]:
        title = str(suggestion.get("title") or "next")
        why = str(suggestion.get("why") or suggestion.get("next") or "")
        lines.append(f"- suggestion: {title}" + (f" — {why}" if why else ""))
    for note in bundle.notes[:5]:
        lines.append(f"- note: {note}")
    return "\n".join(lines)


def _short_data(data: dict[str, Any]) -> str:
    if not data:
        return ""
    preferred = (
        "port",
        "service",
        "product",
        "version",
        "where",
        "url",
        "path",
        "status",
        "evidence",
        "name",
        "value",
    )
    parts: list[str] = []
    for key in preferred:
        if key not in data:
            continue
        value = data[key]
        if value in (None, "", [], {}):
            continue
        parts.append(f"{key}={value}")
        if len(parts) >= 4:
            break
    if not parts:
        # fallback: first two scalar-ish keys
        for key, value in list(data.items())[:2]:
            if isinstance(value, (dict, list)):
                continue
            parts.append(f"{key}={value}")
    text = ", ".join(parts)
    return text if len(text) <= 160 else text[:159] + "…"


def resolve_seed_for_run(
    *,
    targets_info: list[dict[str, Any]] | None,
    seed_arg: str | None,
    no_seed: bool,
    recon_root: Path | None = None,
) -> SeedBundle | None:
    """解析本次 run 的种子；``no_seed`` 关闭；``seed_arg`` 优先；否则自动发现。"""
    if no_seed:
        return None
    if seed_arg:
        return load_seed_path(Path(seed_arg))

    hosts = hosts_from_targets_info(targets_info)
    auto = find_auto_seed(hosts, recon_root=recon_root)
    if auto is None:
        return None
    logger.info("auto-seed: using %s for host(s) %s", auto, ", ".join(hosts) or "(none)")
    return load_seed_path(auto)


def apply_seed_to_prepared(
    *,
    instruction: str | None,
    workspace_files: list[dict[str, Any]],
    bundle: SeedBundle,
) -> tuple[str, list[dict[str, Any]]]:
    """把 brief 拼进 instruction，并把 facts JSON 挂进 workspace_files（content 形态）。

    同时把 SeedFact 落入 task_graph（depth-0），并只派生 recon/enum intent，
    实现「初始阶段少挂载」。
    """
    brief = render_seed_brief(bundle)
    merged_instruction = f"{brief}\n\n{instruction}" if instruction else brief
    payload = json.dumps(bundle.to_payload(), ensure_ascii=False, indent=2).encode("utf-8")
    path = "/workspace/preseed/facts.json"
    # 去掉同路径旧条目，保证幂等。
    kept = [entry for entry in workspace_files if entry.get("workspace_path") != path]
    kept.append({"workspace_path": path, "content": payload})

    # 实际灌图在 runner hydrate_task_graph 之后（见 runner.py）；此处只给 root 提示。
    if bundle.facts:
        hint = (
            "\n\n## DFS intents from seed (recon/enum only)\n"
            "Seed facts will be loaded into the task graph at scan start with "
            "profile `recon` or `enum` only. Prefer "
            "`create_agent(..., intent_id=..., profile=...)` for the next "
            "deepest ready intent rather than spawning a full-toolkit hunter "
            "for rediscovery.\n"
        )
        merged_instruction = f"{merged_instruction}{hint}"

    return merged_instruction, kept
