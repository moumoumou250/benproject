"""对话全量落盘（E0 / transcript）：SDK RunHooks 旁路。

层约束：本模块不得 import ``Ben.tools`` / ``Ben.report`` / ``Ben.interface``。
写失败只记 errors，永不向上抛（控制流异常由 CompositeHooks 原样上抛）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.lifecycle import RunHooks

from Ben.kb.env import env_flag

logger = logging.getLogger(__name__)

_HOOK_NAMES = (
    "on_agent_start",
    "on_agent_end",
    "on_llm_start",
    "on_llm_end",
    "on_tool_start",
    "on_tool_end",
    "on_handoff",
)


def audit_enabled() -> bool:
    return env_flag("BEN_AUDIT")


def _item_kb() -> int:
    try:
        return max(1, int(os.environ.get("BEN_AUDIT_ITEM_KB", "32")))
    except ValueError:
        return 32


def _max_mb() -> int:
    try:
        return max(1, int(os.environ.get("BEN_AUDIT_MAX_MB", "200")))
    except ValueError:
        return 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_item(item: Any) -> str:
    try:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        elif hasattr(item, "dict"):
            item = item.dict()
        return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(item)


def _fingerprint(item: Any) -> str:
    return hashlib.sha1(_canonical_item(item).encode("utf-8")).hexdigest()


def _truncate_text(text: str, *, max_kb: int) -> dict[str, Any]:
    raw = text or ""
    limit = max_kb * 1024
    encoded = raw.encode("utf-8")
    if len(encoded) <= limit:
        return {"text": raw, "chars": len(raw), "sha256": _sha256_text(raw), "truncated": False}
    cut = encoded[:limit].decode("utf-8", errors="ignore")
    return {
        "text": cut,
        "chars": len(raw),
        "full_chars": len(raw),
        "sha256": _sha256_text(raw),
        "truncated": True,
    }


def _serialize_item(item: Any, *, max_kb: int) -> Any:
    try:
        if hasattr(item, "model_dump"):
            payload: Any = item.model_dump()
        elif hasattr(item, "dict"):
            payload = item.dict()
        elif isinstance(item, (dict, list, str, int, float, bool)) or item is None:
            payload = item
        else:
            payload = json.loads(_canonical_item(item))
    except Exception:
        payload = {"repr": repr(item)[:2000]}
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    if len(blob.encode("utf-8")) <= max_kb * 1024:
        return payload
    truncated = _truncate_text(blob, max_kb=max_kb)
    return {
        "truncated": True,
        "full_chars": truncated["full_chars"],
        "sha256": truncated["sha256"],
        "preview": truncated["text"],
    }


@dataclass
class AuditWriter:
    """append-only ``events.jsonl`` + ``index.json``。"""

    audit_dir: Path
    run_name: str = ""
    target: str = ""
    mode: str = "delta"
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _seq: int = 0
    _bytes: int = 0
    _records: int = 0
    _truncated: int = 0
    _errors: int = 0
    _capped: bool = False
    _secret_hits: int = 0
    _agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    _input_fps: dict[str, list[str]] = field(default_factory=dict)
    _system_hash: dict[str, str] = field(default_factory=dict)
    _turns: dict[str, int] = field(default_factory=dict)
    _tool_t0: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.audit_dir, 0o700)
        except OSError:
            logger.debug("audit mkdir failed", exc_info=True)

    @property
    def events_path(self) -> Path:
        return self.audit_dir / "events.jsonl"

    @property
    def index_path(self) -> Path:
        return self.audit_dir / "index.json"

    def summary(self) -> dict[str, Any]:
        return {
            "dir": str(self.audit_dir),
            "events": self._records,
            "agents": len(self._agents),
            "bytes": self._bytes,
            "truncated_items": self._truncated,
            "capped": self._capped,
            "secret_hits": self._secret_hits,
            "errors": self._errors,
            "view": f"ben transcript {self.run_name}" if self.run_name else "ben transcript",
        }

    async def emit(self, event: dict[str, Any]) -> None:
        if self._capped:
            return
        async with self._lock:
            if self._capped:
                return
            self._seq += 1
            event = {"v": 1, "seq": self._seq, "ts": _now_iso(), **event}
            try:
                line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
                data = line.encode("utf-8")
                if (self._bytes + len(data)) > _max_mb() * 1024 * 1024:
                    self._capped = True
                    cap = {
                        "v": 1,
                        "seq": self._seq,
                        "ts": _now_iso(),
                        "type": "capped",
                        "bytes": self._bytes,
                    }
                    await asyncio.to_thread(self._append_bytes, json.dumps(cap).encode() + b"\n")
                    return
                await asyncio.to_thread(self._append_bytes, data)
                self._bytes += len(data)
                self._records += 1
                if event.get("truncated") or (
                    isinstance(event.get("result"), dict) and event["result"].get("truncated")
                ):
                    self._truncated += 1
                if event.get("type") == "llm_start" and event.get("input", {}).get("items"):
                    for it in event["input"]["items"]:
                        if isinstance(it, dict) and it.get("truncated"):
                            self._truncated += 1
            except Exception:
                self._errors += 1
                logger.debug("audit emit failed", exc_info=True)

    def _append_bytes(self, data: bytes) -> None:
        with open(self.events_path, "ab") as fh:
            fh.write(data)
        try:
            os.chmod(self.events_path, 0o600)
        except OSError:
            pass

    async def run_start(self) -> None:
        await self.emit(
            {
                "type": "run_start",
                "run": self.run_name,
                "target": self.target,
                "mode": "full" if env_flag("BEN_AUDIT_FULL") else self.mode,
            }
        )

    async def run_end(self) -> None:
        await self.emit({"type": "run_end", "run": self.run_name})
        await self.flush_index()

    async def flush_index(self) -> None:
        try:
            payload = {
                **self.summary(),
                "run": self.run_name,
                "target": self.target,
                "agents_table": self._agents,
            }
            await asyncio.to_thread(
                self.index_path.write_text,
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )
            try:
                os.chmod(self.index_path, 0o600)
            except OSError:
                pass
        except Exception:
            self._errors += 1
            logger.debug("audit index flush failed", exc_info=True)

    def note_agent(self, agent_id: str, *, name: str, parent: str | None = None) -> None:
        self._agents[agent_id] = {
            "id": agent_id,
            "name": name,
            "parent": parent,
        }

    def next_turn(self, agent_id: str) -> int:
        n = self._turns.get(agent_id, 0) + 1
        self._turns[agent_id] = n
        return n

    def diff_input(self, agent_id: str, items: list[Any]) -> dict[str, Any]:
        fps = [_fingerprint(it) for it in items]
        prev = self._input_fps.get(agent_id, [])
        full = env_flag("BEN_AUDIT_FULL")
        if full or not prev:
            base = 0
            rewind = False
            slice_items = items
        else:
            base = 0
            for a, b in zip(prev, fps):
                if a != b:
                    break
                base += 1
            rewind = len(fps) < len(prev) or base < min(len(prev), len(fps)) and base < len(prev)
            # 若前缀完整等于上轮长度，只写新增
            if base == len(prev) and len(fps) >= len(prev):
                rewind = False
            slice_items = items[base:]
        self._input_fps[agent_id] = fps
        max_kb = _item_kb()
        serialized = [_serialize_item(it, max_kb=max_kb) for it in slice_items]
        return {
            "base": base,
            "total": len(items),
            "rewind": rewind,
            "items": serialized,
        }

    def system_payload(self, agent_id: str, system_prompt: str | None) -> dict[str, Any]:
        text = system_prompt or ""
        digest = _sha256_text(text)
        prev = self._system_hash.get(agent_id)
        changed = prev != digest
        self._system_hash[agent_id] = digest
        out: dict[str, Any] = {
            "system": {"sha256": digest, "chars": len(text)},
            "system_changed": changed,
        }
        if changed:
            out["system_text"] = text
        return out


class AuditHooks(RunHooks[dict[str, Any]]):
    """把 RunHooks 事件写入 AuditWriter。"""

    def __init__(self, writer: AuditWriter) -> None:
        self._w = writer

    def _agent_id(self, context: Any, agent: Any) -> str:
        ctx = getattr(context, "context", None)
        if isinstance(ctx, dict):
            aid = ctx.get("agent_id")
            if isinstance(aid, str) and aid:
                return aid
        name = getattr(agent, "name", None) or "agent"
        return str(name)

    async def on_agent_start(self, context: Any, agent: Any) -> None:
        try:
            aid = self._agent_id(context, agent)
            parent = None
            ctx = getattr(context, "context", None)
            if isinstance(ctx, dict):
                parent = ctx.get("parent_id")
            self._w.note_agent(aid, name=str(getattr(agent, "name", aid)), parent=parent)
            task = ""
            if isinstance(ctx, dict):
                task = str(ctx.get("task") or ctx.get("instruction") or "")[:2000]
            await self._w.emit(
                {
                    "type": "agent_start",
                    "agent": aid,
                    "name": str(getattr(agent, "name", aid)),
                    "parent": parent,
                    "task": task,
                }
            )
        except Exception:
            self._w._errors += 1
            logger.debug("audit on_agent_start failed", exc_info=True)

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        try:
            aid = self._agent_id(context, agent)
            text = output if isinstance(output, str) else repr(output)
            await self._w.emit(
                {
                    "type": "agent_end",
                    "agent": aid,
                    "output": _truncate_text(text, max_kb=_item_kb()),
                }
            )
        except Exception:
            self._w._errors += 1
            logger.debug("audit on_agent_end failed", exc_info=True)

    async def on_llm_start(
        self,
        context: Any,
        agent: Any,
        system_prompt: str | None,
        input_items: list[Any],
    ) -> None:
        try:
            aid = self._agent_id(context, agent)
            turn = self._w.next_turn(aid)
            model = getattr(agent, "model", None)
            model_name = model if isinstance(model, str) else getattr(model, "model", "") or ""
            payload = {
                "type": "llm_start",
                "agent": aid,
                "turn": turn,
                "model": model_name,
                **self._w.system_payload(aid, system_prompt),
                "input": self._w.diff_input(aid, list(input_items or [])),
            }
            await self._w.emit(payload)
        except Exception:
            self._w._errors += 1
            logger.debug("audit on_llm_start failed", exc_info=True)

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        try:
            aid = self._agent_id(context, agent)
            turn = self._w._turns.get(aid, 0)
            usage = {}
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None:
                usage = {
                    "in": getattr(raw_usage, "input_tokens", None)
                    or getattr(raw_usage, "prompt_tokens", None),
                    "out": getattr(raw_usage, "output_tokens", None)
                    or getattr(raw_usage, "completion_tokens", None),
                    "cached": getattr(raw_usage, "cached_tokens", None),
                }
            outputs: list[dict[str, Any]] = []
            for item in getattr(response, "output", None) or []:
                typ = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else "unknown")
                if typ in {"message", "output_text"}:
                    text = getattr(item, "content", None) or getattr(item, "text", None)
                    if isinstance(text, list):
                        parts = []
                        for p in text:
                            parts.append(getattr(p, "text", None) or str(p))
                        text = "".join(str(p) for p in parts)
                    outputs.append(
                        {"type": "message", **_truncate_text(str(text or ""), max_kb=_item_kb())}
                    )
                elif typ in {"function_call", "tool_call"}:
                    outputs.append(
                        {
                            "type": "function_call",
                            "call_id": getattr(item, "call_id", None)
                            or (item.get("call_id") if isinstance(item, dict) else None),
                            "name": getattr(item, "name", None)
                            or (item.get("name") if isinstance(item, dict) else None),
                            "arguments": str(
                                getattr(item, "arguments", None)
                                or (item.get("arguments") if isinstance(item, dict) else "")
                                or ""
                            )[: _item_kb() * 1024],
                        }
                    )
                elif typ == "reasoning":
                    outputs.append(
                        {
                            "type": "reasoning",
                            **_truncate_text(
                                str(getattr(item, "content", None) or item),
                                max_kb=_item_kb(),
                            ),
                        }
                    )
                else:
                    outputs.append({"type": str(typ), "repr": repr(item)[:500]})
            await self._w.emit(
                {
                    "type": "llm_end",
                    "agent": aid,
                    "turn": turn,
                    "usage": usage,
                    "finish": getattr(response, "finish_reason", None),
                    "output": outputs,
                }
            )
        except Exception:
            self._w._errors += 1
            logger.debug("audit on_llm_end failed", exc_info=True)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        try:
            aid = self._agent_id(context, agent)
            call_id = getattr(context, "tool_call_id", None) or ""
            name = getattr(tool, "name", None) or getattr(context, "tool_name", None) or "tool"
            key = f"{aid}:{call_id or name}"
            self._w._tool_t0[key] = time.monotonic()
            args = getattr(context, "tool_arguments", None)
            await self._w.emit(
                {
                    "type": "tool_start",
                    "agent": aid,
                    "call_id": call_id,
                    "tool": str(name),
                    "arguments": _truncate_text(str(args or ""), max_kb=_item_kb()),
                }
            )
        except Exception:
            self._w._errors += 1
            logger.debug("audit on_tool_start failed", exc_info=True)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: object) -> None:
        try:
            aid = self._agent_id(context, agent)
            call_id = getattr(context, "tool_call_id", None) or ""
            name = getattr(tool, "name", None) or getattr(context, "tool_name", None) or "tool"
            key = f"{aid}:{call_id or name}"
            t0 = self._w._tool_t0.pop(key, None)
            duration_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None
            text = result if isinstance(result, str) else repr(result)
            await self._w.emit(
                {
                    "type": "tool_end",
                    "agent": aid,
                    "call_id": call_id,
                    "tool": str(name),
                    "status": "ok",
                    "duration_ms": duration_ms,
                    "result": _truncate_text(text, max_kb=_item_kb()),
                }
            )
        except Exception:
            self._w._errors += 1
            logger.debug("audit on_tool_end failed", exc_info=True)

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        try:
            await self._w.emit(
                {
                    "type": "handoff",
                    "from": str(getattr(from_agent, "name", from_agent)),
                    "to": str(getattr(to_agent, "name", to_agent)),
                }
            )
        except Exception:
            self._w._errors += 1
            logger.debug("audit on_handoff failed", exc_info=True)


def load_events(audit_dir: Path) -> list[dict[str, Any]]:
    path = Path(audit_dir) / "events.jsonl"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


_SECRET_LINE = re.compile(
    r"(?i)^(Cookie|Set-Cookie|Authorization|Proxy-Authorization)\s*:.*$"
)
_SECRET_INLINE = re.compile(
    r"(?i)\b(Bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+|x-api-key\s*[:=]\s*\S+)"
)


def redact(text: str) -> str:
    """导出用脱敏：保留疑似密钥前后各 4 字符。"""

    def _mask_token(tok: str) -> str:
        if len(tok) <= 8:
            return "*" * len(tok)
        return tok[:4] + "…" + tok[-4:]

    lines = []
    for line in (text or "").splitlines():
        if _SECRET_LINE.match(line):
            key, _, rest = line.partition(":")
            lines.append(f"{key}: {_mask_token(rest.strip())}")
            continue
        lines.append(_SECRET_INLINE.sub(lambda m: _mask_token(m.group(0)), line))
    return "\n".join(lines)


def render_markdown(events: list[dict[str, Any]], *, agent: str | None = None, turn: int | None = None) -> str:
    """人读回放。"""
    lines: list[str] = ["# Transcript", ""]
    for ev in events:
        if agent and ev.get("agent") and ev.get("agent") != agent:
            if ev.get("type") not in {"run_start", "run_end", "capped"}:
                continue
        if turn is not None and ev.get("turn") not in (None, turn):
            if ev.get("type") in {"llm_start", "llm_end"}:
                continue
        typ = ev.get("type")
        ts = ev.get("ts", "")
        if typ == "run_start":
            lines.append(f"## Run `{ev.get('run')}` → {ev.get('target')}")
            lines.append("")
        elif typ == "agent_start":
            lines.append(f"### Agent `{ev.get('name')}` (`{ev.get('agent')}`)")
            if ev.get("task"):
                lines.append(f"- task: {ev['task'][:300]}")
            lines.append("")
        elif typ == "llm_start":
            lines.append(f"#### Turn {ev.get('turn')} · llm_start ({ts})")
            if ev.get("system_changed"):
                sys_text = ev.get("system_text") or ""
                lines.append(f"- system changed ({ev.get('system', {}).get('chars')} chars)")
                if sys_text:
                    lines.append("```")
                    lines.append(sys_text[:4000])
                    lines.append("```")
            inp = ev.get("input") or {}
            lines.append(
                f"- input base={inp.get('base')} total={inp.get('total')} rewind={inp.get('rewind')}"
            )
            for it in inp.get("items") or []:
                lines.append(f"  - `{json.dumps(it, ensure_ascii=False)[:500]}`")
            lines.append("")
        elif typ == "llm_end":
            lines.append(f"#### Turn {ev.get('turn')} · llm_end ({ts})")
            for out in ev.get("output") or []:
                if out.get("type") == "message":
                    lines.append(f"- say: {(out.get('text') or '')[:1000]}")
                elif out.get("type") == "function_call":
                    lines.append(
                        f"- call `{out.get('name')}` args={(out.get('arguments') or '')[:500]}"
                    )
            lines.append("")
        elif typ == "tool_end":
            res = ev.get("result") or {}
            lines.append(
                f"- tool `{ev.get('tool')}` → {(res.get('text') or '')[:800]}"
            )
            lines.append("")
        elif typ == "run_end":
            lines.append("## Run end")
            lines.append("")
        elif typ == "capped":
            lines.append("## Capped")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def rebuild_input_at_turn(events: list[dict[str, Any]], agent: str, turn: int) -> list[Any]:
    """按差分事件重建某 agent 第 N 轮的完整 input items。"""
    items: list[Any] = []
    for ev in events:
        if ev.get("type") != "llm_start" or ev.get("agent") != agent:
            continue
        inp = ev.get("input") or {}
        base = int(inp.get("base") or 0)
        chunk = list(inp.get("items") or [])
        if inp.get("rewind"):
            items = items[:base] + chunk
        else:
            items = items[:base] + chunk
        if int(ev.get("turn") or 0) == turn:
            total = int(inp.get("total") or len(items))
            return items[:total]
    return items


def derive_paths_seen(events: list[dict[str, Any]]) -> list[str]:
    """从 tool 参数粗提 URL path（E0 诊断用）。"""
    from urllib.parse import urlparse

    seen: list[str] = []
    url_re = re.compile(r"https?://[^\s\"'`<>]+")
    for ev in events:
        if ev.get("type") != "tool_start":
            continue
        args = (ev.get("arguments") or {}).get("text") if isinstance(ev.get("arguments"), dict) else ""
        blob = str(args or "")
        for u in url_re.findall(blob):
            p = urlparse(u).path or "/"
            if p not in seen:
                seen.append(p)
    return seen


def derive_blocked_snippets(events: list[dict[str, Any]]) -> list[str]:
    needles = ("attention required", "cloudflare", "access denied", "captcha")
    out: list[str] = []
    for ev in events:
        if ev.get("type") != "tool_end":
            continue
        text = ""
        res = ev.get("result")
        if isinstance(res, dict):
            text = str(res.get("text") or "")
        low = text.lower()
        if any(n in low for n in needles):
            out.append(f"turn=? tool={ev.get('tool')}: {text[:200]}")
    return out
