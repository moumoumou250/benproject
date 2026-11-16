"""Key 池 Web API 端到端测试：session 防线、动作语义、key 掩码。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from Ben.config import keypool
from Ben.interface.viewer.server import serve

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture()
def pool_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "keypool.json"
    monkeypatch.setattr(keypool, "pool_path", lambda: path)
    return path


@pytest.fixture()
def viewer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    """一个最小 run 目录 + 已启动的 viewer 服务器，返回 (url, cookie)。"""
    run_dir = tmp_path / "strix_runs" / "demo"
    (run_dir / ".state").mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_name": "demo", "status": "completed", "end_time": "2026-01-01"}),
        encoding="utf-8",
    )
    httpd, url, token = serve(run_dir, open_browser=False)
    try:
        with urllib.request.urlopen(f"{url}/?token={token}") as resp:  # noqa: S310
            raw = str(resp.headers.get("Set-Cookie", ""))
        cookie = raw.split(";", 1)[0]
        yield url, cookie
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str, *, cookie: str | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"Cookie": cookie} if cookie else {}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 - localhost
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - localhost
        return resp.status, json.loads(resp.read())


def _post(
    url: str,
    body: dict[str, Any],
    *,
    cookie: str | None = None,
    origin: str | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(  # noqa: S310 - localhost
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 - localhost
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ---------------------------------------------------------------------------
# 会话防线：无 cookie 一律 403（key 尾号也不许裸露端口读走）
# ---------------------------------------------------------------------------


def test_keypool_get_requires_session(viewer: tuple[str, str]) -> None:
    url, _ = viewer
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(f"{url}/api/keypool")
    assert excinfo.value.code == 403


def test_keypool_post_requires_session(viewer: tuple[str, str]) -> None:
    url, _ = viewer
    status, body = _post(
        f"{url}/api/keypool",
        {"action": "add", "tier": "root", "model": "m", "api_key": "sk-x-12345678"},
    )
    assert status == 403
    assert body["error"] == "forbidden"


def test_keypool_post_rejects_cross_site_origin(viewer: tuple[str, str]) -> None:
    url, cookie = viewer
    status, _body = _post(
        f"{url}/api/keypool",
        {"action": "add", "tier": "root", "model": "m", "api_key": "sk-x-12345678"},
        cookie=cookie,
        origin="http://evil.example",
    )
    assert status == 403


# ---------------------------------------------------------------------------
# 动作语义（带 session）
# ---------------------------------------------------------------------------


def test_keypool_add_switch_clear_flow(viewer: tuple[str, str], pool_file: Path) -> None:
    url, cookie = viewer
    status, body = _post(
        f"{url}/api/keypool",
        {"action": "add", "tier": "root", "model": "deepseek/deepseek-chat", "api_key": "sk-flow-12345678"},
        cookie=cookie,
    )
    assert status == 200 and body["ok"]
    entry = body["entry"]
    assert entry["key_tail"] == "…5678"
    assert "sk-flow-12345678" not in json.dumps(body)

    status, payload = _get(f"{url}/api/keypool", cookie=cookie)
    assert status == 200
    assert payload["root"]["source"] == "keypool"
    assert payload["root"]["entry_id"] == entry["id"]
    assert payload["entries"][0]["active"] is True

    # clear -> 回落 .env
    status, body = _post(f"{url}/api/keypool", {"action": "clear", "tier": "root"}, cookie=cookie)
    assert status == 200
    _, payload = _get(f"{url}/api/keypool", cookie=cookie)
    assert payload["root"]["source"] == "env"

    # switch 回来
    status, body = _post(f"{url}/api/keypool", {"action": "switch", "id": entry["id"]}, cookie=cookie)
    assert status == 200 and body["ok"]
    _, payload = _get(f"{url}/api/keypool", cookie=cookie)
    assert payload["root"]["source"] == "keypool"

    # delete -> 条目与 active 一并消失
    status, body = _post(f"{url}/api/keypool", {"action": "delete", "id": entry["id"]}, cookie=cookie)
    assert status == 200
    _, payload = _get(f"{url}/api/keypool", cookie=cookie)
    assert payload["entries"] == []
    assert payload["root"]["source"] == "env"


def test_keypool_add_validation_error(viewer: tuple[str, str]) -> None:
    url, cookie = viewer
    status, body = _post(f"{url}/api/keypool", {"action": "add", "tier": "root", "model": ""}, cookie=cookie)
    assert status == 400
    assert "模型名" in body["error"]


def test_keypool_unknown_action(viewer: tuple[str, str]) -> None:
    url, cookie = viewer
    status, body = _post(f"{url}/api/keypool", {"action": "explode"}, cookie=cookie)
    assert status == 400
    assert body["error"] == "unknown_action"
