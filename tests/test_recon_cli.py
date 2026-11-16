"""``ben recon``：只读信息收集 CLI。

两类测试分开：

- 纯解析（头部、指纹、证书、建议）—— 不联网、不依赖 curl；
- 端到端 —— 起一个本地 HTTP 服务，用 ``--no-docker`` 走宿主机通道，验证
  "授权不过零请求 / 正常跑出产物 / 不可达退 1"这三条契约。

闸门是唯一依赖 Agent SDK 的部分：默认 monkeypatch 掉（解析测试不该拉整个
agents 栈），另有一条用例在依赖齐备时验证真闸门会拦下范围外命令。
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from Ben.interface.cli import recon
from Ben.interface.cli.recon import (
    _BODY_MARK,
    _HDR_MARK,
    Fact,
    ReconResult,
    Response,
    _Channel,
    _candidate_endpoints,
    _cookie_flags,
    _fingerprint,
    _forms_of,
    _load_paths,
    _parse_cert,
    _parse_headers,
    _suggestions,
    run_recon,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Handler(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        type(self).hits += 1
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /internal\nDisallow: /admin\n"
        elif self.path == "/":
            body = (
                b"<html><head><title>Ben Recon Target</title>"
                b'<script src="/static/app.js"></script></head>'
                b'<body><form action="/login" method="post">'
                b'<input name="user" type="text">'
                b'<input name="pass" type="password"></form></body></html>'
            )
        else:
            body = b"nope"
        status = 200 if self.path in ("/", "/robots.txt") else 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Server", "nginx/1.24.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def live_server() -> Any:
    port = _free_port()
    _Handler.hits = 0
    server = HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析/端到端测试不加载 agents 栈：闸门换成直通。真闸门另有用例。"""
    monkeypatch.setattr(recon, "_gate", lambda script, allowed: None)


class _SpaHandler(BaseHTTPRequestHandler):
    """所有路径都回同一张首页的 SPA —— Juice Shop / Vue/React 前台的典型行为。"""

    index = (
        b"<html><head><title>SPA Shop</title>"
        b'<script src="/static/app.js"></script></head><body>shop</body></html>'
    )

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.index)))
        self.end_headers()
        self.wfile.write(self.index)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def spa_server() -> Any:
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _SpaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()


def _settings(allowed: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        allowed_targets=allowed,
        executor_image="ben-sandbox:dev",
        executor_network="benptnet",
    )


class _FakeEngine:
    """只回放一段 curl 输出，用来验证 marker 解析。"""

    def __init__(self, stdout: str, *, exit_code: int | None = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = stderr
        self.commands: list[list[str]] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def container_name(self) -> str:
        return "benpt-recon-test"

    def execute(self, command: list[str], *, timeout: float = 30.0) -> Any:
        self.commands.append(command)
        return SimpleNamespace(
            stdout=self.stdout, stderr=self.stderr, exit_code=self.exit_code, timed_out=False
        )


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_headers_keeps_last_hop_and_cookies() -> None:
    block = (
        "HTTP/1.1 302 Found\r\nLocation: /login\r\n\r\n"
        "HTTP/1.1 200 OK\r\nServer: nginx\r\nSet-Cookie: sid=abc; Secure; HttpOnly; SameSite=Lax\r\n"
        "Content-Type: text/html\r\n"
    )
    status, headers, cookies, redirects = _parse_headers(block)
    assert status == 200
    assert headers["server"] == "nginx"
    assert cookies and "sid=abc" in cookies[0]
    assert redirects == ["302 → /login"]


def test_fingerprint_reads_header_and_body() -> None:
    items = _fingerprint(
        {"server": "nginx/1.24.0", "x-powered-by": "PHP/8.1.2"},
        "<html>wp-content/themes</html>",
    )
    names = {item["name"]: item["version"] for item in items}
    assert names["nginx"] == "1.24.0"
    assert names["PHP"] == "8.1.2"
    assert "WordPress" in names


def test_candidate_endpoints_skips_assets_and_duplicates() -> None:
    body = 'fetch("/api/users"); x="/api/users"; y="/logo.png"; z="/v1/orders/{id}"'
    found = _candidate_endpoints(body)
    assert "/api/users" in found
    assert found.count("/api/users") == 1
    assert "/logo.png" not in found
    assert "/v1/orders/{id}" in found


def test_forms_capture_fields_and_password_flag() -> None:
    body = (
        "<form action='/login' method='post'>"
        "<input name='user' type='text'><input name='pass' type='password'></form>"
    )
    forms = _forms_of(body)
    assert forms[0]["action"] == "/login"
    assert forms[0]["method"] == "POST"
    assert forms[0]["fields"] == ["user", "pass"]
    assert forms[0]["has_password"] is True


def test_cookie_flags_reads_attributes() -> None:
    flags = _cookie_flags("sid=abc; Path=/; Secure; HttpOnly; SameSite=Strict")
    assert flags == {"name": "sid", "secure": True, "httponly": True, "samesite": "Strict"}


def test_parse_cert_extracts_sans_and_expiry() -> None:
    text = (
        "subject=CN = example.test\n"
        "issuer=C = US, O = Let's Encrypt, CN = R3\n"
        "        X509v3 Subject Alternative Name:\n"
        "            DNS:example.test, DNS:www.example.test\n"
        "Not After : Jun  1 12:00:00 2027 GMT\n"
        "    Protocol  : TLSv1.3\n"
    )
    info = _parse_cert(text)
    assert info["sans"] == ["example.test", "www.example.test"]
    assert info["issuer"].startswith("C = US")
    assert isinstance(info["days_left"], int)


def test_load_paths_from_file(tmp_path: Path) -> None:
    path = tmp_path / "paths.txt"
    path.write_text("# comment\napi\n/admin\n\n", encoding="utf-8")
    args = SimpleNamespace(no_paths=False, path_list=str(path))
    assert _load_paths(args) == ("/api", "/admin")


def test_load_paths_missing_file_raises() -> None:
    args = SimpleNamespace(no_paths=False, path_list="/definitely/missing.txt")
    with pytest.raises(ValueError, match="路径清单不存在"):
        _load_paths(args)


# ---------------------------------------------------------------------------
# 建议
# ---------------------------------------------------------------------------


def _result_with(*facts: Fact) -> ReconResult:
    from datetime import UTC, datetime

    result = ReconResult(
        target="http://t/",
        base_url="http://t/",
        host="t",
        channel="test",
        started_at=datetime.now(UTC),
    )
    for fact in facts:
        result.add(fact)
    return result


def test_suggestions_flag_exposed_git_and_missing_headers() -> None:
    result = _result_with(
        Fact("surface", "/.git/HEAD", {"status": 200}, "paths"),
        Fact("surface", "/admin", {"status": 403}, "paths"),
        Fact(
            "http",
            "安全响应头",
            {"present": [], "missing": ["content-security-policy"]},
            "http-root",
        ),
    )
    titles = [item["title"] for item in _suggestions(result)]
    assert "源码仓库暴露" in titles
    assert "受限端点存在" in titles
    assert "安全响应头缺失" in titles
    # 安全响应头只作建议，措辞必须体现"不认定为漏洞"
    hint = next(item for item in _suggestions(result) if item["title"] == "安全响应头缺失")
    assert "不认定为漏洞" in hint["next"]


def test_suggestions_are_empty_for_a_clean_surface() -> None:
    result = _result_with(Fact("surface", "/admin", {"status": 404}, "paths"))
    assert _suggestions(result) == []


def test_same_page_requires_byte_identical_body() -> None:
    page = "<html><title>Shop</title>" + "x" * 200 + "</html>"
    baseline = Response(url="http://t/ben-missing-abc", status=200, body=page)
    assert recon._is_same_page(Response(url="http://t/.env", status=200, body=page), baseline)
    # 长度相近但内容不同 → 不是兜底页（模板插了路径/时间戳也要算不同）
    assert not recon._is_same_page(
        Response(url="http://t/metrics", status=200, body=page + "metrics"), baseline
    )
    # 状态码不同 → 不认
    assert not recon._is_same_page(Response(url="http://t/x", status=404, body=page), baseline)
    # 基线没拿到 → 不做任何"相同"判定（否则等于把所有 200 都当兜底）
    assert not recon._is_same_page(Response(url="http://t/x", status=200, body=page), None)


def test_fallback_paths_never_become_exposure_suggestions() -> None:
    """Juice Shop 场景：SPA 对任何路径都回 200，不能报成 .git/.env 泄露。"""
    result = _result_with(
        Fact(
            "surface",
            "/ben-missing-aaaaaa",
            {"status": 200, "bytes": 9393, "baseline": True},
            "baseline",
        ),
        Fact("surface", "/.git/HEAD", {"status": 200, "bytes": 9393, "fallback": True}, "paths"),
        Fact("surface", "/.env", {"status": 200, "bytes": 9393, "fallback": True}, "paths"),
        Fact("surface", "/backup.zip", {"status": 200, "bytes": 9393, "fallback": True}, "paths"),
    )
    titles = [item["title"] for item in _suggestions(result)]
    assert "源码仓库暴露" not in titles
    assert "敏感文件可直接下载" not in titles
    assert any("软 404" in title for title in titles)


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------


def test_channel_fetch_parses_markers(no_gate: None) -> None:
    stdout = (
        f"\n{_HDR_MARK}\n"
        "HTTP/1.1 200 OK\r\nServer: nginx/1.24.0\r\nContent-Type: text/html\r\n\r\n"
        f"\n{_BODY_MARK}\n"
        "<html><title>Hi</title></html>"
    )
    engine = _FakeEngine(stdout)
    response = _Channel(engine, allowed=("http://t/",), timeout=5).fetch("http://t/")
    assert response.status == 200
    assert response.header("server") == "nginx/1.24.0"
    assert "<title>Hi</title>" in response.body
    assert engine.commands and engine.commands[0][:2] == ["sh", "-c"]


def test_channel_fetch_reports_curl_failure(no_gate: None) -> None:
    engine = _FakeEngine("", exit_code=7, stderr="Failed to connect")
    response = _Channel(engine, allowed=("http://t/",), timeout=5).fetch("http://t/")
    assert response.status is None
    assert "TCP 连接被拒绝" in response.error


def test_channel_gate_blocks_off_scope_command() -> None:
    pytest.importorskip("agents")
    channel = _Channel(None, allowed=("http://127.0.0.1:9/",), timeout=1)
    with pytest.raises(recon.GateError):
        channel.shell("curl -sS -- http://not-allowed.test/")


def test_fetch_script_passes_the_real_gate() -> None:
    """取页面的那段脚本必须能过 Agent 同款闸门，否则一次都跑不起来。

    这条用例是防回归的关键：``BENRC=$?`` / ``exit $BENRC`` 这类写法一旦被
    间接引用闸门当成绕过，整个 recon 会在真实环境里 100% 失败。
    """
    pytest.importorskip("agents")
    script = recon.fetch_script("http://127.0.0.1:3000/api/items?page=1&size=2", 5)
    # 不抛 GateError 就是通过
    recon._gate(script, ("http://127.0.0.1:3000",))


def test_fetch_script_still_blocks_off_scope_url() -> None:
    pytest.importorskip("agents")
    script = recon.fetch_script("http://not-allowed.test/", 5)
    with pytest.raises(recon.GateError):
        recon._gate(script, ("http://127.0.0.1:3000",))


def test_tls_script_passes_the_real_gate() -> None:
    """读证书那条命令同样要过闸门：``</dev/null`` 与 ``2>/dev/null`` 不能被当成绕过。"""
    pytest.importorskip("agents")
    recon._gate(recon.tls_script("127.0.0.1", 8443), ("http://127.0.0.1:8443",))
    recon._gate(recon.tls_script("portal.example.test", 443), ("https://portal.example.test",))


def test_tls_script_skips_sni_for_ip_targets() -> None:
    assert "-servername" not in recon.tls_script("10.0.0.5", 443)
    assert "-servername portal.example.test" in recon.tls_script("portal.example.test", 443)


def test_parse_cert_verbose_reads_curl_output() -> None:
    text = (
        "* SSL connection using TLSv1.3 / TLS_AES_256_GCM_SHA384\n"
        "* Server certificate:\n"
        "*  subject: CN=portal.example.test\n"
        "*  start date: Jan  1 00:00:00 2026 GMT\n"
        "*  expire date: Jun  1 12:00:00 2027 GMT\n"
        "*  issuer: C=US; O=Let's Encrypt; CN=R3\n"
    )
    info = recon._parse_cert_verbose(text)
    assert info["subject"] == "CN=portal.example.test"
    assert info["issuer"].startswith("C=US")
    assert info["protocol"] == "TLSv1.3"
    assert info["sans"] == []
    assert isinstance(info["days_left"], int)


def test_verbose_script_passes_the_real_gate() -> None:
    pytest.importorskip("agents")
    recon._gate(
        recon.fetch_verbose_script("https://portal.example.test/", 5),
        ("https://portal.example.test",),
    )


def test_save_raw_skips_binary(no_gate: None, tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    instance = recon.Recon(
        target="http://t/",
        base_url="http://t/",
        host="t",
        allowed=("http://t/",),
        channel=_Channel(None, allowed=(), timeout=1),
        paths=(),
        max_paths=0,
        max_js=0,
        frontend=False,
        raw_dir=raw_dir,
    )
    instance._save_raw(
        "bin",
        Response(
            url="http://t/x.bin",
            status=200,
            headers={"content-type": "application/octet-stream"},
            body="\x00\x01",
        ),
    )
    instance._save_raw(
        "page",
        Response(url="http://t/", status=200, headers={"content-type": "text/html"}, body="<html>"),
    )
    assert [p.name for p in raw_dir.iterdir()] == ["page.txt"]


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="本机没有 curl")


@needs_curl
def test_recon_collects_and_writes_artifacts(
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
    no_gate: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kw: _settings((live_server,)))
    out = tmp_path / "recon-out"
    code = run_recon([live_server, "--no-docker", "--out", str(out), "--max-paths", "3"])

    assert code == 0
    payload = json.loads((out / "recon.json").read_text(encoding="utf-8"))
    kinds = {fact["kind"] for fact in payload["facts"]}
    assert {"host", "http", "surface"} <= kinds
    root = next(f for f in payload["facts"] if f["kind"] == "http" and f["title"] == "根路径响应")
    assert root["data"]["status"] == 200
    assert root["data"]["title"] == "Ben Recon Target"
    assert (out / "recon.md").is_file()
    captured = capsys.readouterr().out
    assert "Ben 信息收集" in captured


@needs_curl
def test_recon_refuses_unauthorized_before_any_request(
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
    no_gate: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kw: _settings(()))
    _Handler.hits = 0
    code = run_recon([live_server, "--no-docker", "--out", str(tmp_path / "x")])

    assert code == 2
    assert _Handler.hits == 0  # 授权不过 = 零网络动作
    assert "未发出任何请求" in capsys.readouterr().out
    assert not (tmp_path / "x").exists()


@needs_curl
def test_recon_unreachable_returns_one(
    monkeypatch: pytest.MonkeyPatch, no_gate: None, tmp_path: Path
) -> None:
    port = _free_port()
    target = f"http://127.0.0.1:{port}/"
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kw: _settings((target,)))
    code = run_recon([target, "--no-docker", "--timeout", "1", "--out", str(tmp_path / "y")])
    assert code == 1


@needs_curl
def test_recon_json_mode_prints_only_json(
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
    no_gate: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kw: _settings((live_server,)))
    code = run_recon(
        [live_server, "--no-docker", "--json", "--no-write", "--no-paths", "--no-frontend"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert re.match(r"^\s*\{", out)
    payload = json.loads(out[out.index("{") :])
    assert payload["url"].startswith("http://127.0.0.1")


@needs_curl
def test_recon_does_not_report_spa_catch_all_as_exposure(
    spa_server: str,
    monkeypatch: pytest.MonkeyPatch,
    no_gate: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """端到端复现 Juice Shop：全站回同一张首页时，/.git/HEAD 不能算泄露。"""
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kw: _settings((spa_server,)))
    out = tmp_path / "spa"
    code = run_recon(
        [spa_server, "--no-docker", "--out", str(out), "--max-paths", "8", "--no-frontend"]
    )
    assert code == 0

    captured = capsys.readouterr().out
    assert "源码仓库暴露" not in captured
    assert "敏感文件可直接下载" not in captured
    assert "软 404" in captured

    payload = json.loads((out / "recon.json").read_text(encoding="utf-8"))
    probed = [f for f in payload["facts"] if f["source"] == "paths"]
    assert probed and all(f["data"]["fallback"] for f in probed)
    baseline = next(f for f in payload["facts"] if f["data"].get("baseline"))
    assert baseline["data"]["status"] == 200
    # 兜底页的原文不该进 raw/：那会让人以为真的拿到了 /.env（根页是正常证据，保留）
    raw = out / "raw"
    saved = {path.name for path in raw.iterdir()} if raw.exists() else set()
    assert saved <= {"root.txt"}
