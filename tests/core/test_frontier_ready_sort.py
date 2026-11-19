"""Ready 按 risk / 状态码 / 静态面排序。"""

from __future__ import annotations

from Ben.core.frontier import ready_sort_key, render_frontier_block


def test_ready_sort_key_prefers_auth_over_static_enum() -> None:
    admin = {
        "surface": "/admin",
        "risk_area": "auth",
        "outcome": "pending",
        "detail": "inventory:paths status=401",
    }
    robots = {
        "surface": "/robots.txt",
        "risk_area": "enum",
        "outcome": "pending",
        "detail": "inventory:paths status=200",
    }
    assert ready_sort_key(admin) < ready_sort_key(robots)


def test_ready_block_shows_admin_before_robots() -> None:
    entries = [
        {
            "entry_id": "r1",
            "surface": "/robots.txt",
            "risk_area": "enum",
            "outcome": "pending",
            "evidence": "inventory:paths status=200",
        },
        {
            "entry_id": "a1",
            "surface": "/admin",
            "risk_area": "auth",
            "outcome": "pending",
            "evidence": "inventory:paths status=401",
        },
        {
            "entry_id": "s1",
            "surface": "/style.css",
            "risk_area": "enum",
            "outcome": "pending",
            "evidence": "inventory:paths status=200",
        },
    ]
    block = render_frontier_block(coordinator=None, entries=entries)
    ready = block.split("## Ready")[1].split("##")[0]
    assert ready.index("/admin") < ready.index("/robots.txt")
    assert ready.index("/admin") < ready.index("/style.css")
