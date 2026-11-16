"""verify 队列：HTTP 判定、入队、outcome。"""

from __future__ import annotations

from pathlib import Path

import pytest

from Ben.tools.verify.tool import (
    _finding_looks_http,
    get_verify_result,
    hydrate_verify_from_disk,
    record_verify_result,
    reset_verify_store,
    unfinished_verify_ids,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path) -> None:
    reset_verify_store()
    hydrate_verify_from_disk(tmp_path)
    yield
    reset_verify_store()


def test_http_detection() -> None:
    assert _finding_looks_http({"http_request_id": "x"}) is True
    assert _finding_looks_http({"metadata": {"exchange_id": "y"}}) is True
    assert _finding_looks_http({"title": "logic bug"}) is False


def test_record_and_unfinished() -> None:
    record_verify_result("v2", status="queued", reason="pending")
    assert "v2" in unfinished_verify_ids()
    record_verify_result("v2", status="upheld", reason="ok")
    assert unfinished_verify_ids() == []
    assert get_verify_result("v2")["status"] == "upheld"
