"""从 recon/scout 产物抽出 Inventory，供 coverage pending 灌入。

只读解析，不发网络请求。默认每面一个主 risk_area；对带查询串或写方法的
面额外拆 xss/sqli（有条件多 risk，避免全局一对多爆炸）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)

__all__ = [
    "ALIVE_STATUSES",
    "infer_risk_area",
    "inventory_from_facts",
    "inventory_from_path",
    "inventory_from_seed_payload",
    "risk_areas_for_surface",
]

ALIVE_STATUSES: frozenset[int] = frozenset({200, 201, 204, 301, 302, 401, 403, 405})

_AUTH_PATH = re.compile(
    r"(?:^|/)(?:login|logout|signin|sign-in|signup|sign-up|register|auth|oauth|"
    r"admin|administrator|account|session|password|reset)(?:/|$|\?)",
    re.IGNORECASE,
)
_API_PATH = re.compile(r"(?:^|/)(?:api|graphql|rest|v\d+)(?:/|$|\?)", re.IGNORECASE)
_WRITE_METHOD = re.compile(r"^(?:POST|PUT|PATCH|DELETE)\s+", re.IGNORECASE)
_MAX_RISKS_PER_SURFACE = 3


def infer_risk_area(surface: str) -> str:
    """路径语义 → 单一主 risk。顺序：auth > authz > enum。"""
    text = (surface or "").strip()
    path = text
    if "://" in text:
        path = urlsplit(text).path or text
    # 去掉方法前缀（POST /api/...）
    if " " in path and path.split(" ", 1)[0].isalpha():
        path = path.split(" ", 1)[1]
    if _AUTH_PATH.search(path):
        return "auth"
    if _API_PATH.search(path):
        return "authz"
    return "enum"


def _has_input_surface(surface: str) -> bool:
    """带查询参数或写方法 → 值得额外测注入类。"""
    text = (surface or "").strip()
    if not text:
        return False
    if _WRITE_METHOD.match(text):
        return True
    if "?" in text:
        return True
    return False


def risk_areas_for_surface(surface: str) -> list[str]:
    """主 risk + 有条件附加（最多 3 个）。无输入点则仅主 risk。"""
    primary = infer_risk_area(surface)
    risks = [primary]
    if _has_input_surface(surface):
        for extra in ("xss", "sqli"):
            if extra not in risks:
                risks.append(extra)
            if len(risks) >= _MAX_RISKS_PER_SURFACE:
                break
    return risks[:_MAX_RISKS_PER_SURFACE]


def _surface_label(data: dict[str, Any], title: str) -> str:
    url = str(data.get("url") or "").strip()
    if url:
        return url
    path = str(data.get("path") or "").strip()
    if path:
        return path
    return (title or "").strip()


def _row(
    *,
    surface: str,
    risk_area: str,
    kind: str,
    discovered_by: str,
    evidence: str,
) -> dict[str, Any]:
    return {
        "surface": surface,
        "risk_area": risk_area,
        "kind": kind,
        "discovered_by": discovered_by,
        "evidence": evidence,
    }


def _alive_surface_items(fact: dict[str, Any]) -> list[dict[str, Any]]:
    data = fact.get("data") if isinstance(fact.get("data"), dict) else {}
    if data.get("fallback") is True or data.get("baseline") is True:
        return []
    status = data.get("status")
    if status is None:
        return []
    try:
        status_i = int(status)
    except (TypeError, ValueError):
        return []
    if status_i not in ALIVE_STATUSES:
        return []
    surface = _surface_label(data, str(fact.get("title") or ""))
    if not surface:
        return []
    source = str(fact.get("source") or "recon")
    evidence = f"inventory:{source} status={status_i}"
    return [
        _row(
            surface=surface,
            risk_area=risk,
            kind="surface",
            discovered_by=source,
            evidence=evidence,
        )
        for risk in risk_areas_for_surface(surface)
    ]


def _endpoint_items(fact: dict[str, Any]) -> list[dict[str, Any]]:
    data = fact.get("data") if isinstance(fact.get("data"), dict) else {}
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return []
    source = str(fact.get("source") or "frontend")
    out: list[dict[str, Any]] = []
    for item in raw_items:
        surface = str(item or "").strip()
        if not surface:
            continue
        for risk in risk_areas_for_surface(surface):
            out.append(
                _row(
                    surface=surface,
                    risk_area=risk,
                    kind="endpoint",
                    discovered_by=source,
                    evidence=f"inventory:endpoint from {source}",
                )
            )
    return out


def _service_item(fact: dict[str, Any]) -> dict[str, Any] | None:
    data = fact.get("data") if isinstance(fact.get("data"), dict) else {}
    port = data.get("port")
    if port is None and isinstance(data.get("items"), list):
        # fingerprint 类 service 没有单端口，跳过
        return None
    if port is None:
        return None
    host = str(data.get("host") or data.get("hostname") or fact.get("title") or "").strip()
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return None
    surface = f"{host}:{port_i}" if host else f"port:{port_i}"
    source = str(fact.get("source") or "scout")
    return _row(
        surface=surface,
        risk_area="enum",
        kind="service",
        discovered_by=source,
        evidence=f"inventory:service port={port_i} from {source}",
    )


def inventory_from_facts(facts: list[Any]) -> list[dict[str, Any]]:
    """从 fact 列表抽出待灌 pending 的行（已按 surface+risk 去重，保序）。"""
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []

    def _add(item: dict[str, Any] | None) -> None:
        if item is None:
            return
        key = (item["surface"].strip().lower(), item["risk_area"].strip().lower())
        if key in seen or not key[0]:
            return
        seen.add(key)
        rows.append(item)

    for fact in facts:
        if not isinstance(fact, dict):
            continue
        kind = str(fact.get("kind") or "").strip().lower()
        if kind == "surface":
            for item in _alive_surface_items(fact):
                _add(item)
        elif kind == "endpoint":
            for item in _endpoint_items(fact):
                _add(item)
        elif kind == "service":
            _add(_service_item(fact))
    return rows


def inventory_from_seed_payload(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return inventory_from_facts(payload)
    if not isinstance(payload, dict):
        return []
    facts = payload.get("facts")
    if isinstance(facts, list):
        return inventory_from_facts(facts)
    return []


def inventory_from_path(path: Path) -> list[dict[str, Any]]:
    """读 recon.json / scout.json / facts.json。失败返回空列表。"""
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("inventory: cannot read %s: %s", path, exc)
        return []
    if isinstance(payload, (dict, list)):
        return inventory_from_seed_payload(payload)
    return []
