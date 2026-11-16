"""recall：token overlap 与 task 拼接。"""

from __future__ import annotations

from Ben.core.recall import (
    augment_task_with_recall,
    format_recall_block,
    overlap_score,
    tokenize,
)
from Ben.tools.coverage.tools import _coverage_lock, _coverage_storage


def setup_function() -> None:
    with _coverage_lock:
        _coverage_storage.clear()


def teardown_function() -> None:
    with _coverage_lock:
        _coverage_storage.clear()


def test_tokenize_cjk_and_ascii() -> None:
    tokens = tokenize("测试 XSS payload /login")
    assert "测" in tokens or "试" in tokens
    assert "xss" in tokens
    assert "login" in tokens


def test_overlap_and_augment() -> None:
    with _coverage_lock:
        _coverage_storage["e1"] = {
            "surface": "/api/login",
            "risk_area": "xss",
            "outcome": "ruled_out",
            "evidence": "escaped reflection",
        }
    task = "Probe /api/login for xss reflection"
    block = format_recall_block(task)
    assert "已知/已排除" in block
    assert "xss" in block.lower() or "login" in block.lower()
    augmented = augment_task_with_recall(task)
    assert task in augmented
    assert "已知/已排除" in augmented
    # idempotent-ish when already present
    assert augment_task_with_recall(augmented) == augmented


def test_low_overlap_skipped() -> None:
    assert overlap_score({"a"}, {"b"}) == 0.0
    with _coverage_lock:
        _coverage_storage["e1"] = {
            "surface": "unrelated-zzzz",
            "risk_area": "other",
            "outcome": "ruled_out",
            "evidence": "zzz",
        }
    assert format_recall_block("totally different query about widgets") == ""
