"""``ben scout``：侦察兵 CLI（端口 / 版本台账 / 入口面 / 裁定 / 导出）。

分两类：

- 纯函数（nmap 解析、产品版本拆分、台账合并、裁定打分、sapper 导出）——不联网；
- 端到端 —— 本地 HTTP 服务 + ``--no-docker --no-ports``，验证三条契约：
  "授权不过零请求 / 正常跑出 scout.json 与裁定 / 导出能被 shlex 正确还原"。

闸门在 ``_Channel.shell`` 里，测试默认 monkeypatch 掉（解析测试不该拉整个 agents
栈）；另有用例在依赖齐备时验证真闸门会拦下范围外的 nmap/nc 命令。
"""

from __future__ import annotations

import json
import shlex
import shutil
import socket
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from Ben.interface.cli import recon, scout
from Ben.interface.cli.recon import Fact, ReconResult
from Ben.interface.cli.scout import (
    PortObservation,
    _banner_product_version,
    _entry_face,
    _parse_nmap_greppable,
    _parse_ports,
    _payload_policy,
    _split_product_version,
    _verdict,
    _version_ledger,
    _version_locked,
    run_scout,
    sapper_commands,
    scan_script,
)


# ---------------------------------------------------------------------------
# 工具与夹具
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
            body = b"User-agent: *\nDisallow: /internal\n"
            status = 200
        elif self.path == "/":
            body = (
                b"<html><head><title>Ben Scout Target</title></head>"
                b'<body><form action="/login" method="post">'
                b'<input name="user" type="text">'
                b'<input name="pass" type="password"></form></body></html>'
            )
            status = 200
        elif self.path in ("/actuator", "/login", "/api"):
            body = b"ok"
            status = 200
        else:
            body = b"nope"
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Server", "nginx/1.24.0")
        self.send_header("X-Powered-By", "PHP/8.1.2")
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
    monkeypatch.setattr(recon, "_gate", lambda script, allowed: None)


def _settings(allowed: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        allowed_targets=allowed,
        executor_image="ben-sandbox:dev",
        executor_network="benptnet",
    )


def _result_with(*facts: Fact, host: str = "10.0.0.5") -> ReconResult:
    result = ReconResult(
        target="http://10.0.0.5/",
        base_url="http://10.0.0.5/",
        host=host,
        channel="test",
        started_at=datetime.now(UTC),
    )
    for fact in facts:
        result.add(fact)
    return result


def _observation(port: int, *, service: str = "", product: str = "", version: str = "") -> Any:
    return PortObservation(
        port=port,
        state="open",
        service=service,
        product=product,
        version=version,
        evidence="test",
    )


class _StubChannel:
    """按命令前缀返回预设结果，用来验证"工具不可用时怎么办"这类分支。"""

    label = "stub"

    def __init__(self, table: list[tuple[str, tuple[str, str, int]]]) -> None:
        self.table = table
        self.commands: list[str] = []

    def shell(self, script: str, *, timeout: float | None = None) -> tuple[str, str, int]:
        self.commands.append(script)
        for needle, response in self.table:
            if needle in script:
                return response
        return "", "", 0


def _scout_with(channel: _StubChannel, *, ports: tuple[int, ...] = (80, 443)) -> scout.Scout:
    instance = scout.Scout(
        target="http://10.0.0.5/",
        base_url="http://10.0.0.5/",
        host="10.0.0.5",
        allowed=("http://10.0.0.5/",),
        channel=channel,  # type: ignore[arg-type]
        paths=(),
        max_paths=0,
        max_js=0,
        frontend=False,
        raw_dir=None,
        minutes=4.0,
        ports=ports,
        port_timeout=1.0,
    )
    instance.deadline = time.monotonic() + 120
    return instance


# ---------------------------------------------------------------------------
# 工具可用性：nmap 的 file capabilities 与容器 bounding set 冲突
# ---------------------------------------------------------------------------

_EPERM = (
    "",
    "/usr/bin/nmap: 6: exec: /usr/lib/nmap/nmap: Operation not permitted",
    126,
)
_NMAP_OK = ("Nmap version 7.99 ( https://nmap.org )", "", 0)


def test_nmap_falls_back_to_capability_free_copy() -> None:
    # 注意先匹配 cp：副本命令串里也含 "nmap --version"
    channel = _StubChannel([("cp -f", _NMAP_OK), ("nmap --version", _EPERM)])
    instance = _scout_with(channel)
    prefix = instance._nmap_prefix()
    assert prefix == f"NMAPDIR={scout._NMAP_DATA} {scout._NMAP_SHIM}"
    assert any("无特权副本" in note for note in instance.result.notes)
    # 后续扫描必须真的用上副本，而不是又跑一次必然失败的 nmap
    assert scan_script("10.0.0.5", (80,), timeout=5, nmap=prefix).startswith("NMAPDIR=")


def test_nmap_reports_unavailable_when_shim_also_fails() -> None:
    channel = _StubChannel([("nmap --version", _EPERM), ("cp -f", ("", "cp: cannot stat", 1))])
    instance = _scout_with(channel)
    assert instance._nmap_prefix() is None
    assert any("nmap 不可用" in note for note in instance.result.notes)


def test_ports_degrade_to_nc_instead_of_silently_reporting_zero() -> None:
    channel = _StubChannel(
        [
            ("cp -f", ("", "cp: cannot stat", 1)),
            ("nmap --version", _EPERM),
            ("command -v nc", ("/bin/nc\n", "", 0)),
            ("nc -z", ("", "", 0)),
        ]
    )
    instance = _scout_with(channel)
    instance._step_ports()
    open_ports = [obs.port for obs in instance.observations if obs.state == "open"]
    assert open_ports == [80, 443]
    assert any("nc -z" in command for command in channel.commands)
    assert any("nmap 不可用" in note for note in instance.result.notes)


# ---------------------------------------------------------------------------
# 版本台账：web 面与旁路服务分开算
# ---------------------------------------------------------------------------


def test_version_scan_is_per_port_and_capped() -> None:
    """逐端口扫，且按上限截断：一批被超时掐掉不能连累其他端口。"""
    greppable = "Host: 10.0.0.5 ()\tPorts: 22/open/tcp//ssh//OpenSSH 8.9p1/\n"
    channel = _StubChannel([("nmap -Pn -sT -n -sV", (greppable, "", 0))])
    instance = _scout_with(channel, ports=tuple(range(1, 21)))
    instance._step_service_versions("10.0.0.5", tuple(range(1, 21)), "nmap")
    calls = [cmd for cmd in channel.commands if "-sV" in cmd]
    assert len(calls) == scout._VERSION_MAX_PORTS
    # 每个调用只带一个端口
    assert all("-p " in cmd and "," not in cmd.split("-p ")[1].split()[0] for cmd in calls)
    assert any(obs.product == "OpenSSH" for obs in instance.observations)


def test_version_lock_ignores_network_service_versions() -> None:
    """ssh 版本不能把 web 目标算成"已钉死"——否则会误判 easy 并直接派兵。"""
    result = _result_with(
        Fact(
            "service",
            "10.0.0.5:22/tcp",
            {
                "host": "10.0.0.5",
                "port": 22,
                "proto": "tcp",
                "state": "open",
                "service": "ssh",
                "product": "OpenSSH",
                "version": "8.9p1",
                "evidence": "nmap -sV OpenSSH 8.9p1",
            },
            "nmap",
        ),
        Fact(
            "service",
            "10.0.0.5:3000/tcp",
            {
                "host": "10.0.0.5",
                "port": 3000,
                "proto": "tcp",
                "state": "open",
                "service": "http",
                "evidence": "nmap -sV",
            },
            "nmap",
        ),
    )
    ledger = _version_ledger(result)
    roles = {item["product"]: item["role"] for item in ledger}
    assert roles["OpenSSH"] == "network"
    assert not _version_locked(ledger)


def test_json_version_reads_public_version_endpoints() -> None:
    assert scout._json_version('{"version": "19.0.0"}') == "19.0.0"
    assert scout._json_version('{"commit":"abc","database":"ok","version":"10.4.1"}') == "10.4.1"
    assert scout._json_version("<html>not found</html>") == ""


def test_app_version_step_pins_product_from_page_title() -> None:
    channel = _StubChannel([])
    channel.fetch = lambda url: recon.Response(  # type: ignore[method-assign]
        url=url, status=200, body='{"version": "17.1.1"}'
    )
    instance = _scout_with(channel)
    instance.result.add(Fact("http", "根路径响应", {"title": "OWASP Juice Shop"}, "root"))
    instance._step_app_version()
    ledger = _version_ledger(instance.result)
    pinned = {item["product"]: item["version"] for item in ledger}
    assert pinned["OWASP Juice Shop"] == "17.1.1"
    assert _version_locked(ledger)
    assert any("17.1.1" in note for note in instance.result.notes)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_ports_accepts_lists_and_ranges() -> None:
    assert _parse_ports("22,80,443") == (22, 80, 443)
    assert _parse_ports("80-82") == (80, 81, 82)
    # 重复值只留一份：同一端口扫两遍会产出两条事实
    assert _parse_ports("80,80") == (80,)


def test_parse_ports_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        _parse_ports("http")
    with pytest.raises(ValueError):
        _parse_ports("99999")


def test_scout_paths_prioritise_entry_points() -> None:
    """截断/时间盒不够时，先丢的必须是"泄露类"，不是判定要用的入口类。"""
    paths = scout._scout_paths(SimpleNamespace(no_paths=False, path_list=None))
    assert paths[:3] == ("/login", "/admin", "/api")
    assert "/actuator" in paths[: len(scout._ENTRY_PATHS)]
    assert "/.env" in paths
    assert scout._scout_paths(SimpleNamespace(no_paths=True, path_list=None)) == ()


def test_split_product_version_keeps_multiword_products() -> None:
    assert _split_product_version("nginx 1.24.0") == ("nginx", "1.24.0")
    # Server 头的常见写法是 product/version
    assert _split_product_version("nginx/1.24.0") == ("nginx", "1.24.0")
    assert _split_product_version("OpenSSH 8.9p1 Ubuntu 3ubuntu0.4") == ("OpenSSH", "8.9p1")
    assert _split_product_version("Apache httpd 2.4.41 ((Ubuntu))") == (
        "Apache httpd",
        "2.4.41",
    )
    # 认不出版本时整串当产品，绝不截半个版本号出来
    assert _split_product_version("Microsoft Windows RPC") == ("Microsoft Windows RPC", "")


def test_nmap_greppable_parses_states_and_versions() -> None:
    text = (
        "Host: 10.0.0.5 ()\tStatus: Up\n"
        "Host: 10.0.0.5 ()\tPorts: 22/open/tcp//ssh//OpenSSH 8.9p1 Ubuntu 3ubuntu0.4/, "
        "80/open/tcp//http//nginx 1.24.0/, 3306/closed/tcp//mysql///\n"
        "Host: 10.0.0.5 ()\tIgnored State: filtered (1)\n"
    )
    observations = _parse_nmap_greppable(text)
    by_port = {obs.port: obs for obs in observations}
    assert set(by_port) == {22, 80, 3306}
    assert by_port[22].version == "8.9p1"
    assert by_port[80].product == "nginx"
    assert by_port[3306].state == "closed"
    assert by_port[3306].version == ""


def test_banner_product_version_reads_ssh_and_ftp() -> None:
    assert _banner_product_version("SSH-2.0-OpenSSH_8.9p1 Ubuntu-3") == ("OpenSSH", "8.9p1")
    assert _banner_product_version("220 (vsFTPd 3.0.3)")[1] == "3.0.3"
    assert _banner_product_version("no idea what this is")[0] == ""


# ---------------------------------------------------------------------------
# 版本台账与策略
# ---------------------------------------------------------------------------


def test_version_ledger_merges_and_keeps_highest_confidence() -> None:
    result = _result_with(
        Fact(
            "service",
            "技术栈指纹",
            {"items": [{"name": "nginx", "version": "", "category": "server"}]},
            "fingerprint",
        ),
        Fact(
            "service",
            "10.0.0.5:80/tcp",
            {
                "host": "10.0.0.5",
                "port": 80,
                "service": "http",
                "product": "nginx",
                "version": "1.24.0",
            },
            "nmap -sV",
        ),
    )
    ledger = _version_ledger(result)
    assert len(ledger) == 1  # 同一个产品只留一行
    assert ledger[0]["version"] == "1.24.0"
    assert ledger[0]["confidence"] == "high"
    assert _version_locked(ledger)


def test_version_not_locked_when_version_unknown() -> None:
    result = _result_with(
        Fact(
            "service",
            "技术栈指纹",
            {"items": [{"name": "WordPress", "version": "", "category": "cms"}]},
            "fingerprint",
        )
    )
    ledger = _version_ledger(result)
    assert ledger[0]["product"] == "WordPress"
    assert not _version_locked(ledger)
    policy = _payload_policy(False)
    assert policy["policy"] == "no-blind-payload"
    assert "kb052" in policy["why"]


# ---------------------------------------------------------------------------
# 裁定
# ---------------------------------------------------------------------------


def _verdict_for(
    result: ReconResult,
    *,
    observations: list[PortObservation] | None = None,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ledger = _version_ledger(result)
    return _verdict(
        result,
        ledger=ledger,
        entries=entries if entries is not None else _entry_face(result),
        observations=observations or [],
        elapsed=12.0,
        minutes=4.0,
    )


def test_verdict_easy_when_version_locked_and_debug_endpoint() -> None:
    result = _result_with(
        Fact(
            "service",
            "技术栈指纹",
            {"items": [{"name": "nginx", "version": "1.24.0", "category": "server"}]},
            "fingerprint",
        ),
        Fact("surface", "/actuator", {"status": 200}, "paths"),
    )
    verdict = _verdict_for(result)
    assert verdict["level"] == "easy"
    assert verdict["confidence"] == "high"
    assert verdict["version_locked"] is True
    assert "soldier" in verdict["next_step"]
    assert any(signal["toward"] == "easy" for signal in verdict["signals"])


def test_verdict_hard_without_entries_or_version() -> None:
    result = _result_with(
        Fact("surface", "/admin", {"status": 404}, "paths"),
        Fact("surface", "/login", {"status": 404}, "paths"),
    )
    verdict = _verdict_for(result)
    assert verdict["level"] == "hard"
    assert verdict["version_locked"] is False
    assert "general" in verdict["next_step"]
    # 每条判定都必须写清方向与理由，否则无法复核
    assert verdict["signals"]
    assert all(signal["signal"] for signal in verdict["signals"])


def test_verdict_is_low_confidence_on_soft_404_site() -> None:
    result = _result_with(
        Fact(
            "surface",
            "/ben-missing-abc",
            {"status": 200, "bytes": 900, "baseline": True},
            "baseline",
        ),
        Fact("surface", "/.env", {"status": 200, "bytes": 900, "fallback": True}, "paths"),
        Fact("surface", "/login", {"status": 200, "bytes": 900, "fallback": True}, "paths"),
    )
    verdict = _verdict_for(result)
    assert verdict["confidence"] == "low"
    assert "scout-again" in verdict["next_step"]


def test_entry_face_treats_5xx_as_route_exists() -> None:
    """5xx 说明路由命中了 handler（否则会落到兜底页），不能按"不存在"处理。"""
    result = _result_with(
        Fact("surface", "/api", {"status": 500, "bytes": 2408}, "paths"),
        Fact("surface", "/api/docs", {"status": 503, "bytes": 2418}, "paths"),
    )
    entries = _entry_face(result)
    assert {item["type"] for item in entries} == {"error"}
    verdict = _verdict_for(result, entries=entries)
    assert any("5xx" in signal["signal"] for signal in verdict["signals"])
    assert verdict["score_easy"] >= 1


def test_soft_404_does_not_void_confirmed_hits() -> None:
    """兜底页与真实命中可以并存：有逐字节不同的命中时，不该整片否定路径结论。"""
    result = _result_with(
        Fact(
            "surface",
            "/ben-missing-abc",
            {"status": 200, "bytes": 900, "baseline": True},
            "baseline",
        ),
        Fact("surface", "/.env", {"status": 200, "bytes": 900, "fallback": True}, "paths"),
        Fact("surface", "/login", {"status": 200, "bytes": 900, "fallback": True}, "paths"),
        Fact("surface", "/admin", {"status": 200, "bytes": 900, "fallback": True}, "paths"),
        Fact("surface", "/metrics", {"status": 200, "bytes": 26313}, "paths"),
    )
    verdict = _verdict_for(result)
    assert not any("路径类结论不可信" in signal["signal"] for signal in verdict["signals"])
    assert any("有效命中并存" in note for note in result.notes)
    # 有真实命中就不该仅仅因为兜底页把置信度压到 low
    assert verdict["confidence"] != "low"


def test_entry_face_collects_debug_auth_and_network_entries() -> None:
    result = _result_with(
        Fact("surface", "/actuator", {"status": 200}, "paths"),
        Fact("surface", "/admin", {"status": 403}, "paths"),
        Fact(
            "param",
            "页面表单",
            {
                "forms": [
                    {
                        "action": "/login",
                        "method": "POST",
                        "fields": ["u", "p"],
                        "has_password": True,
                    }
                ]
            },
            "frontend",
        ),
        Fact(
            "service",
            "10.0.0.5:6379/tcp",
            {"host": "10.0.0.5", "port": 6379, "proto": "tcp", "state": "open", "service": "redis"},
            "nmap",
        ),
    )
    types = {item["type"] for item in _entry_face(result)}
    assert {"debug", "admin", "auth", "network"} <= types


# ---------------------------------------------------------------------------
# sapper 导出
# ---------------------------------------------------------------------------


def test_sapper_commands_are_shell_safe_and_skip_soft_404() -> None:
    result = _result_with(
        Fact(
            "service",
            "10.0.0.5:22/tcp",
            {
                "host": "10.0.0.5",
                "port": 22,
                "service": "ssh",
                "product": "OpenSSH",
                "version": "8.9p1",
            },
            "nmap -sV",
        ),
        Fact("surface", "/.env", {"status": 200, "bytes": 9, "fallback": True}, "paths"),
        Fact(
            "note", "难度裁定", {"level": "easy", "reason": "版本已钉死；调试端点暴露"}, "verdict"
        ),
    )
    lines = sapper_commands(result)
    # 软 404 不许上图：它和随机基线逐字节相同，写进去等于骗下一个 agent
    assert all(".env" not in line for line in lines)
    service_line = next(line for line in lines if "service:10.0.0.5:22/tcp" in line)
    tokens = shlex.split(service_line)
    assert tokens[:3] == ["sapper", "fact", "service:10.0.0.5:22/tcp"]
    assert "-k" in tokens and tokens[tokens.index("-k") + 1] == "service"
    assert "host=10.0.0.5" in tokens
    assert "port=22" in tokens
    verdict_line = next(line for line in lines if "难度裁定" in line)
    assert tokens_kind(verdict_line, "-k") == "note"


def tokens_kind(line: str, flag: str) -> str:
    tokens = shlex.split(line)
    return tokens[tokens.index(flag) + 1]


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------


def test_scan_script_passes_the_real_gate() -> None:
    pytest.importorskip("agents")
    recon._gate(scan_script("10.0.0.5", (22, 80), timeout=30), ("http://10.0.0.5",))
    recon._gate(scout.nc_probe_script("10.0.0.5", 22, timeout=2), ("http://10.0.0.5",))
    recon._gate(scout.banner_script("10.0.0.5", 22, timeout=2), ("http://10.0.0.5",))


def test_scan_script_still_blocks_off_scope_host() -> None:
    pytest.importorskip("agents")
    with pytest.raises(recon.GateError):
        recon._gate(scan_script("not-allowed.test", (80,), timeout=30), ("http://10.0.0.5",))


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="本机没有 curl")


@needs_curl
def test_scout_writes_verdict_and_sapper_export(
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
    no_gate: None,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kw: _settings((live_server,)))
    out = tmp_path / "scout-out"
    code = run_scout(
        [
            live_server,
            "--no-docker",
            "--no-ports",
            "--no-frontend",
            "--minutes",
            "2",
            "--max-paths",
            "6",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    payload = json.loads((out / "scout.json").read_text(encoding="utf-8"))
    assert payload["verdict"]["level"] in ("easy", "medium", "hard")
    assert payload["verdict"]["version_locked"] is True  # Server: nginx/1.24.0
    titles = {fact["title"] for fact in payload["facts"]}
    assert {"版本台账", "入口面", "难度裁定"} <= titles
    # 四件产物齐全：JSON / 人读 / jsonl / sapper 脚本
    for name in ("scout.json", "scout.md", "facts.jsonl", "facts.sapper.sh"):
        assert (out / name).is_file()
    sapper = (out / "facts.sapper.sh").read_text(encoding="utf-8")
    assert "sapper fact" in sapper and "-k service" in sapper


@needs_curl
def test_scout_refuses_unauthorized_before_any_request(
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
    no_gate: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kw: _settings(()))
    _Handler.hits = 0
    code = run_scout([live_server, "--no-docker", "--no-ports", "--out", str(tmp_path / "x")])

    assert code == 2
    assert _Handler.hits == 0  # 授权不过 = 零网络动作
    assert "未发出任何请求" in capsys.readouterr().out
    assert not (tmp_path / "x").exists()


@needs_curl
def test_scout_emit_sapper_prints_only_commands(
    live_server: str,
    monkeypatch: pytest.MonkeyPatch,
    no_gate: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kw: _settings((live_server,)))
    code = run_scout(
        [
            live_server,
            "--no-docker",
            "--no-ports",
            "--no-frontend",
            "--no-write",
            "--emit",
            "sapper",
            "--max-paths",
            "4",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out and all(line.startswith("sapper fact ") for line in out)
    assert any("-k service" in line for line in out)
