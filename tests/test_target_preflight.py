"""目标预检 CLI：宿主机探测、授权判定、扫描前的闸门。

不碰 Docker：要么显式 ``--no-docker``，要么把 :class:`_ContainerEngine`
替换成桩。测试只关心"结论对不对、退出码对不对"。
"""

from __future__ import annotations

import importlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from Ben.interface.cli import target_check
from Ben.interface.cli.target_check import (
    Probe,
    _authorized,
    _host_of,
    _host_view_url,
    check_run_targets,
    run_preflight,
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:  # 静音访问日志
        return


@pytest.fixture()
def live_server() -> Any:
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


def _patch_settings(monkeypatch: pytest.MonkeyPatch, allowed: tuple[str, ...]) -> None:
    settings = SimpleNamespace(
        allowed_targets=allowed,
        executor_image="ghcr.io/example/sandbox:test",
        executor_network="benptnet",
    )
    monkeypatch.setattr("Ben.config.load_settings", lambda **_kwargs: settings)


# ---------------------------------------------------------------------------
# 主机解析与授权
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("http://127.0.0.1:3000/", "127.0.0.1"),
        ("https://Staging.Example.COM/api", "staging.example.com"),
        ("example.com", "example.com"),
        ("10.0.0.5:8080", "10.0.0.5"),
        ("git@github.com:org/repo.git", "github.com"),
        ("/home/kali/code/project", ""),
    ],
)
def test_host_of(target: str, expected: str) -> None:
    assert _host_of(target) == expected


def test_allowlist_accepts_schemeless_entries() -> None:
    """工具层闸门与预检同一口径：``b.example`` 这种裸域名也要能放行命令。"""
    from Ben.agents.factory import _allowed_hostnames

    hosts = _allowed_hostnames(
        ("http://a.example", "b.example", "c.example:8080", "10.0.0.5", "/local/path")
    )
    assert {"a.example", "b.example", "c.example", "10.0.0.5"} <= hosts
    assert "." not in hosts


def test_local_directory_is_not_judged_by_the_host_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: Any
) -> None:
    """本地源码不走主机白名单（工具层只在命令里出现主机时才查）。"""
    project = tmp_path / "proj"
    project.mkdir()
    _patch_settings(monkeypatch, ("http://127.0.0.1:3000",))

    assert run_preflight([str(project), "--no-docker"]) == 0
    out = capsys.readouterr().out
    assert "local_code 目标不走主机白名单" in out
    assert "结论 OK" in out


def test_authorized_treats_localhost_aliases_as_one_group() -> None:
    allowed = ("http://127.0.0.1:3000",)
    ok, detail = _authorized("host.docker.internal", allowed)
    assert ok is True
    assert "同属授权范围" in detail

    ok, detail = _authorized("localhost", allowed)
    assert ok is True

    ok, detail = _authorized("evil.example.com", allowed)
    assert ok is False
    assert "不在授权白名单内" in detail
    assert "127.0.0.1" in detail


def test_host_view_url_keeps_localhost_for_the_host_side() -> None:
    # 容器内地址被改写成 host.docker.internal，宿主机探测必须退回原地址
    assert (
        _host_view_url("http://localhost:3000", "http://host.docker.internal:3000")
        == "http://localhost:3000"
    )
    assert _host_view_url("10.0.0.5", "http://10.0.0.5:8080") == "http://10.0.0.5:8080"


# ---------------------------------------------------------------------------
# CLI：真实起一个本地 HTTP 服务
# ---------------------------------------------------------------------------


def test_reachable_target_passes(
    monkeypatch: pytest.MonkeyPatch, live_server: int, capsys: Any
) -> None:
    url = f"http://127.0.0.1:{live_server}/"
    _patch_settings(monkeypatch, (url,))

    assert run_preflight([url, "--no-docker", "--timeout", "2"]) == 0
    out = capsys.readouterr().out
    assert "HTTP 200" in out
    assert "结论 OK" in out


def test_closed_port_fails_with_actionable_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    port = _free_port()  # 拿到一个当前没人监听的端口
    url = f"http://127.0.0.1:{port}/"
    _patch_settings(monkeypatch, (url,))

    assert run_preflight([url, "--no-docker", "--timeout", "1"]) == 1
    out = capsys.readouterr().out
    assert "连接被拒绝" in out
    assert "结论 FAIL" in out
    assert "--skip-preflight" in out


def test_unauthorized_target_still_gets_probed(
    monkeypatch: pytest.MonkeyPatch, live_server: int, capsys: Any
) -> None:
    """白名单外不等于不可达：用户问"站通不通"，答案不能被授权状态掩盖。

    可达但未授权退出 2，与"不可达"（1）区分开。
    """
    url = f"http://127.0.0.1:{live_server}/"
    _patch_settings(monkeypatch, ("https://allowed.example.com",))

    assert run_preflight([url, "--no-docker", "--timeout", "2"]) == 2
    out = capsys.readouterr().out
    assert "HTTP 200" in out  # 仍然做了真实探测
    assert "不在授权白名单内" in out
    assert "可达 · 未授权" in out
    assert "结论 WARN" in out


def test_unreachable_and_unauthorized_reports_both(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _patch_settings(monkeypatch, ("https://allowed.example.com",))

    assert run_preflight(["http://10.9.9.9:8080", "--no-docker", "--timeout", "1"]) == 1
    out = capsys.readouterr().out
    assert "不在授权白名单内" in out
    assert "结论 FAIL" in out


def test_invalid_target_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _patch_settings(monkeypatch, ("http://127.0.0.1:3000",))

    assert run_preflight(["definitely-not-a-target", "--no-docker"]) == 1
    out = capsys.readouterr().out
    assert "classify" in out
    assert "FAIL" in out


def test_json_output_has_verdict_per_target(
    monkeypatch: pytest.MonkeyPatch, live_server: int, capsys: Any
) -> None:
    url = f"http://127.0.0.1:{live_server}/"
    _patch_settings(monkeypatch, (url,))

    assert run_preflight([url, "--no-docker", "--json", "--timeout", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["targets"][0]["reachable"] is True
    assert payload["targets"][0]["authorized"] is True
    assert payload["targets"][0]["ready"] is True
    assert payload["targets"][0]["type"] == "web_application"
    checks = {item["check"] for item in payload["targets"][0]["checks"]}
    assert {"authorize", "probe-host"} <= checks


def test_no_targets_and_empty_allowlist_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _patch_settings(monkeypatch, ())

    assert run_preflight(["--no-docker"]) == 1
    assert "没有可检查的目标" in capsys.readouterr().out


def test_targets_default_to_allowlist_when_omitted(
    monkeypatch: pytest.MonkeyPatch, live_server: int, capsys: Any
) -> None:
    url = f"http://127.0.0.1:{live_server}/"
    _patch_settings(monkeypatch, (url,))

    assert run_preflight(["--no-docker", "--timeout", "2"]) == 0
    assert url in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 扫描前的闸门
# ---------------------------------------------------------------------------


class _FailingEngine:
    """容器视角探测失败：模拟靶场没起（117-72-199-2-3000 那次）。"""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.closed = False

    def prepare(self) -> Probe:
        return Probe("docker", "ok", "Docker 27.0 · 网络 benptnet")

    def curl(self, url: str, timeout: float) -> Probe:
        return Probe("probe-container", "fail", "TCP 连接被拒绝（curl exit=7）", "靶场容器可能已退出")

    def curl_ports(self, ip: str, ports: tuple[int, ...], timeout: float) -> Probe:
        return self.curl(f"http://{ip}/", timeout)

    def close(self) -> None:
        self.closed = True


def test_run_gate_blocks_unreachable_target(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _patch_settings(monkeypatch, ("http://127.0.0.1:1",))
    monkeypatch.setattr(target_check, "_ContainerEngine", _FailingEngine)

    args = SimpleNamespace(
        targets_info=[{"type": "web_application", "original": "http://127.0.0.1:1"}],
        preflight_timeout=1,
    )
    assert check_run_targets(args) == 1
    out = capsys.readouterr().out
    assert "已拒绝启动扫描" in out
    assert "没有创建 run" in out


def test_run_gate_blocks_unauthorized_target_even_when_reachable(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """通不通不是放行条件：不在白名单就一定会被工具层拒绝，必须拦。"""

    class _HealthyEngine(_FailingEngine):
        def curl(self, url: str, timeout: float) -> Probe:
            return Probe("probe-container", "ok", "容器内直连成功 · HTTP 200")

    _patch_settings(monkeypatch, ("https://allowed.example.com",))
    monkeypatch.setattr(target_check, "_ContainerEngine", _HealthyEngine)

    args = SimpleNamespace(
        targets_info=[{"type": "web_application", "original": "http://127.0.0.1:1"}],
        preflight_timeout=1,
    )
    assert check_run_targets(args) == 1
    out = capsys.readouterr().out
    assert "不在授权范围" in out
    assert "ALLOWED_TARGETS" in out


def test_run_gate_skips_local_and_repository_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, ("http://127.0.0.1:1",))
    args = SimpleNamespace(
        targets_info=[
            {"type": "local_code", "original": "/tmp/project"},
            {"type": "repository", "original": "https://github.com/org/repo"},
        ],
        preflight_timeout=1,
    )
    assert check_run_targets(args) is None


def test_run_gate_passes_when_container_sees_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HealthyEngine(_FailingEngine):
        def curl(self, url: str, timeout: float) -> Probe:
            return Probe("probe-container", "ok", "容器内直连成功 · HTTP 200 · 对端 127.0.0.1")

    _patch_settings(monkeypatch, ("http://127.0.0.1:1",))
    monkeypatch.setattr(target_check, "_ContainerEngine", _HealthyEngine)

    args = SimpleNamespace(
        targets_info=[{"type": "web_application", "original": "http://127.0.0.1:1"}],
        preflight_timeout=1,
    )
    # 宿主机视角可能失败，但容器视角可达即放行
    assert check_run_targets(args) is None


def test_run_gate_does_not_block_without_a_container_view(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """容器视角拿不到时，宿主机视角的失败不足以拒绝启动（172.19.0.2 类目标）。"""

    class _NoDockerEngine(_FailingEngine):
        def prepare(self) -> Probe:
            return Probe("docker", "skip", "Docker 不可用：daemon not running")

    _patch_settings(monkeypatch, ("http://127.0.0.1:1",))
    monkeypatch.setattr(target_check, "_ContainerEngine", _NoDockerEngine)

    args = SimpleNamespace(
        targets_info=[{"type": "web_application", "original": "http://127.0.0.1:1"}],
        preflight_timeout=1,
    )
    assert check_run_targets(args) is None
    assert "闸门放行" in capsys.readouterr().out


def test_run_task_stops_at_the_gate_without_preparing_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """闸门失败时不能走到 prepare_run（不建 run、不克隆仓库、不调模型）。"""
    cli_main: Any = importlib.import_module("Ben.interface.main")
    from Ben.interface.cli import environment, scan_setup

    calls: list[str] = []
    monkeypatch.setattr(environment, "validate_environment", lambda: calls.append("env"))
    monkeypatch.setattr(environment, "pull_docker_image", lambda: calls.append("pull"))

    def _blocked(_args: Any) -> int:
        calls.append("gate")
        return 1

    def _prepare(_args: Any) -> dict[str, Any]:
        calls.append("prepare_run")
        return {}

    monkeypatch.setattr(target_check, "check_run_targets", _blocked)
    monkeypatch.setattr(scan_setup, "prepare_run", _prepare)

    args = SimpleNamespace(
        targets_info=[{"type": "web_application", "original": "http://127.0.0.1:1"}],
        preflight_timeout=1,
    )
    assert cli_main._run_task(args) == 1
    assert calls == ["env", "pull", "gate"]


def test_run_task_skips_the_gate_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_main: Any = importlib.import_module("Ben.interface.main")
    from Ben.interface.cli import environment, scan_setup

    calls: list[str] = []
    monkeypatch.setattr(environment, "validate_environment", lambda: calls.append("env"))
    monkeypatch.setattr(environment, "pull_docker_image", lambda: calls.append("pull"))
    monkeypatch.setattr(target_check, "check_run_targets", lambda _args: calls.append("gate"))

    def _prepare(_args: Any) -> dict[str, Any]:
        calls.append("prepare_run")
        raise scan_setup.PreparationError("stop here")

    monkeypatch.setattr(scan_setup, "prepare_run", _prepare)

    args = SimpleNamespace(targets_info=[], skip_preflight=True)
    assert cli_main._run_task(args) == 1
    assert calls == ["env", "pull", "prepare_run"]
