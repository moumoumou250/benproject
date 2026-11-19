"""证据采集（E1/E2）与 surface_hit。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class EvidenceKind(StrEnum):
    E0 = "e0"
    E1 = "e1"
    E2 = "e2"
    E3 = "e3"
    E4 = "e4"


_CONFIRMABLE = frozenset({EvidenceKind.E1, EvidenceKind.E2})


@dataclass(frozen=True)
class Evidence:
    path: str  # 相对 evidence_dir
    kind: EvidenceKind
    method: str = ""
    url_path: str = ""
    status: int | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            object.__setattr__(self, "kind", EvidenceKind(str(self.kind)))


def coerce_kind(value: EvidenceKind | str) -> EvidenceKind:
    return value if isinstance(value, EvidenceKind) else EvidenceKind(str(value))


def is_confirmable_kind(kind: EvidenceKind | str) -> bool:
    return coerce_kind(kind) in _CONFIRMABLE


def _norm_path(url_or_path: str) -> str:
    raw = (url_or_path or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return urlparse(raw).path or "/"
    return raw.split("?", 1)[0] or "/"


_HTTP_METHODS = frozenset(
    {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
        "TRACE",
        "CONNECT",
    }
)


def parse_req_first_line(text: str) -> tuple[str, str] | None:
    """解析 ``*.req`` 首行：``METHOD /path`` 或 ``METHOD https://...``。"""
    first = (text or "").splitlines()[0] if text else ""
    m = re.match(r"^([A-Za-z]+)\s+(\S+)", first.strip())
    if not m:
        return None
    method = m.group(1).upper()
    if method not in _HTTP_METHODS:
        return None
    return method, m.group(2)


def surface_hit(evidence: list[Evidence], oracle_path: str, *, method: str | None = None) -> bool:
    """扫描期 **e1/e2** 报文是否打到 oracle.path（不算 oracle/precheck、不算 e3）。"""
    want = _norm_path(oracle_path)
    if not want:
        return False
    for ev in evidence:
        if not is_confirmable_kind(ev.kind):
            continue
        if ev.path.startswith("oracle/"):
            continue
        got = _norm_path(ev.url_path)
        if got == want or got.rstrip("/") == want.rstrip("/"):
            if method and ev.method and ev.method.upper() != method.upper():
                continue
            return True
    return False


def paths_seen(evidence: list[Evidence]) -> list[str]:
    """仅 e1/e2 路径（可证明请求过）。"""
    seen: list[str] = []
    for ev in evidence:
        if not is_confirmable_kind(ev.kind):
            continue
        p = _norm_path(ev.url_path)
        if p and p not in seen and not ev.path.startswith("oracle/"):
            seen.append(p)
    return seen


_URL_IN_TEXT = re.compile(r"https?://[^\s\"'`<>]+", re.I)
_PATHISH = re.compile(r"(?:^|[\s\"'=])(/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{2,})")


def paths_seen_from_agents_db(run_dir: Path) -> list[str]:
    """从 ``.state/agents.db`` 的 **function_call.arguments** 抠 URL/path。

    只读；失败返回空列表。不得当作 e1/e2；仅作 ``paths_hint_db``。
    """
    from Ben.core.paths import runtime_state_dir

    db = runtime_state_dir(run_dir) / "agents.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    seen: list[str] = []
    try:
        try:
            rows = conn.execute("select message_data from agent_messages order by id")
        except sqlite3.Error:
            return []
        for (data,) in rows:
            try:
                payload = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                continue
            for chunk in _function_call_argument_texts(payload):
                for u in _URL_IN_TEXT.findall(chunk):
                    p = _norm_path(u)
                    if p and p not in seen:
                        seen.append(p)
                for m in _PATHISH.finditer(chunk):
                    p = _norm_path(m.group(1))
                    if p.count("/") >= 2 and p not in seen:
                        seen.append(p)
    finally:
        conn.close()
    return seen


def _function_call_argument_texts(payload: Any) -> list[str]:
    out: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            out.extend(_function_call_argument_texts(item))
        return out
    if not isinstance(payload, dict):
        return out
    typ = str(payload.get("type") or "").lower()
    if typ in {"function_call", "function_call_item", "tool_call"}:
        args = payload.get("arguments")
        if isinstance(args, str) and len(args) >= 4:
            out.append(args)
        elif isinstance(args, dict):
            try:
                out.append(json.dumps(args, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        return out
    for key in ("item", "message", "data", "content"):
        nested = payload.get(key)
        if isinstance(nested, (dict, list)):
            out.extend(_function_call_argument_texts(nested))
    return out


_LAST_HTTP_PARSE_ERRORS: list[str] = []


def last_http_parse_errors() -> list[str]:
    return list(_LAST_HTTP_PARSE_ERRORS)


def collect_from_http_dir(
    http_dir: Path, *, kind: EvidenceKind | str = EvidenceKind.E2
) -> list[Evidence]:
    """从 evidence 目录递归收集 ``*.req``。"""
    global _LAST_HTTP_PARSE_ERRORS
    out: list[Evidence] = []
    parse_errors: list[str] = []
    if not http_dir.is_dir():
        _LAST_HTTP_PARSE_ERRORS = []
        return out
    root_name = http_dir.name
    ek = coerce_kind(kind)
    for req_path in sorted(http_dir.rglob("*.req")):
        if not req_path.is_file():
            continue
        try:
            rel = req_path.relative_to(http_dir).as_posix()
        except ValueError:
            rel = req_path.name
        text = req_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_req_first_line(text)
        if parsed is None:
            parse_errors.append(f"{root_name}/{rel}")
            method, url = "", ""
        else:
            method, url = parsed
        status = None
        resp = req_path.with_suffix(".resp")
        if resp.is_file():
            head = resp.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
            sm = re.search(r"(\d{3})", head[0] if head else "")
            if sm:
                status = int(sm.group(1))
        note = "parse_error" if parsed is None else ""
        out.append(
            Evidence(
                path=f"{root_name}/{rel}",
                kind=ek,
                method=method,
                url_path=_norm_path(url),
                status=status,
                note=note,
            )
        )
    _LAST_HTTP_PARSE_ERRORS = parse_errors
    return out


_HTTP_FENCE = re.compile(r"```http\s*\n(.*?)```", re.I | re.S)


def materialize_finding_exchanges(run_dir: Path, dest_dir: Path) -> list[Evidence]:
    """finding ```http`` → findings_http/*.req|*.resp，一律 **e3**。"""
    path = run_dir / "vulnerabilities.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []

    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[Evidence] = []
    idx = len(list(dest_dir.glob("*.req"))) + 1
    rel_prefix = dest_dir.name

    for item in data:
        if not isinstance(item, dict):
            continue
        endpoint = str(item.get("endpoint") or "").strip()
        method = str(item.get("method") or "GET").upper()
        blob = str(item.get("evidence") or item.get("poc") or item.get("detail") or "")
        fences = _HTTP_FENCE.findall(blob)
        if not fences and endpoint:
            out.append(
                Evidence(
                    path=f"findings/{item.get('id') or 'x'}.endpoint",
                    kind=EvidenceKind.E3,
                    method=method,
                    url_path=_norm_path(endpoint),
                    note="endpoint only",
                )
            )
            continue
        for block in fences:
            req_text, resp_text = _split_http_exchange(block)
            if not req_text:
                continue
            parsed = parse_req_first_line(req_text)
            meth = (parsed[0] if parsed else method).upper()
            url = parsed[1] if parsed else endpoint
            safe = _norm_path(url).strip("/").replace("/", "_") or "root"
            stem = f"{idx:03d}_{meth}_{safe}"
            (dest_dir / f"{stem}.req").write_text(req_text, encoding="utf-8")
            (dest_dir / f"{stem}.resp").write_text(
                resp_text or "HTTP/1.1 000\n\n", encoding="utf-8"
            )
            status = None
            sm = re.search(r"HTTP/\d\.\d\s+(\d{3})", resp_text or "")
            if sm:
                status = int(sm.group(1))
            out.append(
                Evidence(
                    path=f"{rel_prefix}/{stem}.req",
                    kind=EvidenceKind.E3,
                    method=meth,
                    url_path=_norm_path(url),
                    status=status,
                    note="from finding.evidence http fence",
                )
            )
            idx += 1
    return out


def _split_http_exchange(block: str) -> tuple[str, str]:
    text = block.strip("\n")
    parts = re.split(r"\n(?=HTTP/\d\.\d\s)", text, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip() + "\n", parts[1].strip() + "\n"
    return text + "\n", ""


def collect_from_findings(run_dir: Path) -> list[Evidence]:
    """E3 辅助：endpoint / poc URL（不得单独支撑 CONFIRMED）。"""
    path = run_dir / "vulnerabilities.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Evidence] = []
    if not isinstance(data, list):
        return out
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        endpoint = str(item.get("endpoint") or "").strip()
        method = str(item.get("method") or "").upper()
        if endpoint:
            out.append(
                Evidence(
                    path=f"findings[{i}].endpoint",
                    kind=EvidenceKind.E3,
                    method=method,
                    url_path=_norm_path(endpoint),
                    note="from vulnerabilities.json endpoint",
                )
            )
        poc = str(item.get("poc") or item.get("detail") or item.get("evidence") or "")
        urls = re.findall(r"https?://[^\s\"'`]+", poc)
        for j, u in enumerate(urls[:5]):
            out.append(
                Evidence(
                    path=f"findings[{i}].poc#{j}",
                    kind=EvidenceKind.E3,
                    url_path=_norm_path(u),
                    note="from vulnerabilities.json",
                )
            )
    return out


def blocked_features(text: str) -> bool:
    """粗检 WAF/拦截页。"""
    low = (text or "").lower()
    needles = (
        "attention required",
        "cloudflare",
        "access denied",
        "request blocked",
        "captcha",
        "cf-ray",
    )
    return any(n in low for n in needles)
