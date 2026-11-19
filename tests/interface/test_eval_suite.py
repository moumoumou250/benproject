"""ben eval recall helpers."""

from __future__ import annotations

import json
from pathlib import Path

from Ben.interface.cli.eval_suite import evaluate


def test_evaluate_recall(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "vulnerabilities.json").write_text(
        json.dumps(
            [
                {"title": "SQL injection on login", "detail": "payload"},
                {"title": "Reflected XSS", "detail": "xss alert"},
            ]
        ),
        encoding="utf-8",
    )
    (run / "coverage.json").write_text(
        json.dumps(
            {
                "not_covered": {
                    "count": 1,
                    "items": [
                        {
                            "surface": "/admin",
                            "risk_area": "auth",
                            "outcome": "pending",
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    suite = {
        "name": "t",
        "expected": [
            {"id": "sqli", "match": ["sql", "login"]},
            {"id": "rce", "match": ["remote code"]},
        ],
        "surfaces": ["/admin"],
    }
    result = evaluate(suite=suite, run_dir=run)
    assert result["hits"] == ["sqli"]
    assert result["misses"] == ["rce"]
    assert result["recall"] == 0.5
    assert result["uncovered_surfaces"] == 1
