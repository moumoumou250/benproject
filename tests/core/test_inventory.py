"""Inventory extraction from recon/scout facts."""

from __future__ import annotations

from Ben.core.inventory import (
    infer_risk_area,
    inventory_from_facts,
    inventory_from_seed_payload,
    risk_areas_for_surface,
)


def test_infer_risk_area_single() -> None:
    assert infer_risk_area("/login") == "auth"
    assert infer_risk_area("http://x/api/users") == "authz"
    assert infer_risk_area("/robots.txt") == "enum"


def test_conditional_multi_risk_for_query_and_write() -> None:
    assert risk_areas_for_surface("/robots.txt") == ["enum"]
    assert risk_areas_for_surface("/search?q=1") == ["enum", "xss", "sqli"]
    assert risk_areas_for_surface("/api/users?id=1") == ["authz", "xss", "sqli"]
    assert risk_areas_for_surface("POST /api/orders") == ["authz", "xss", "sqli"]
    assert risk_areas_for_surface("/login") == ["auth"]


def test_alive_surface_and_endpoint() -> None:
    facts = [
        {
            "kind": "surface",
            "title": "/admin",
            "data": {"url": "http://t/admin", "status": 401},
            "source": "paths",
        },
        {
            "kind": "surface",
            "title": "/gone",
            "data": {"url": "http://t/gone", "status": 404},
            "source": "paths",
        },
        {
            "kind": "surface",
            "title": "/fallback",
            "data": {"url": "http://t/x", "status": 200, "fallback": True},
            "source": "paths",
        },
        {
            "kind": "endpoint",
            "title": "候选",
            "data": {"items": ["/api/orders", "/api/orders"], "count": 2},
            "source": "frontend",
        },
        {
            "kind": "service",
            "title": "ssh",
            "data": {"host": "t", "port": 22},
            "source": "scout",
        },
    ]
    rows = inventory_from_facts(facts)
    surfaces = {r["surface"] for r in rows}
    assert "http://t/admin" in surfaces
    assert "http://t/gone" not in surfaces
    assert "http://t/x" not in surfaces
    assert "/api/orders" in surfaces
    assert "t:22" in surfaces
    admin = next(r for r in rows if r["surface"] == "http://t/admin")
    assert admin["risk_area"] == "auth"
    assert admin["evidence"].startswith("inventory:")


def test_seed_payload_dict() -> None:
    rows = inventory_from_seed_payload(
        {"facts": [{"kind": "surface", "title": "/a", "data": {"url": "/a", "status": 200}}]}
    )
    assert len(rows) == 1
