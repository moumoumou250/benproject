from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from Ben.interface.viewer.launcher import ScanLauncher
from Ben.interface.viewer.server import serve


def test_log_write_failure_has_actionable_error(tmp_path):
    launcher = ScanLauncher(tmp_path)
    with (
        patch.object(Path, "open", side_effect=PermissionError("access denied")),
        pytest.raises(RuntimeError, match="报告目录写入权限"),
    ):
        launcher.start({"target": "http://example.com"})
    assert launcher.process is None


@pytest.mark.parametrize(
    "target",
    [
        "file:///etc/passwd",
        "https://user:pass@example.com",
        "http://example.com:invalid",
        "http://example.com/ bad",
        "--help",
    ],
)
def test_reject_invalid_target(tmp_path, target):
    with pytest.raises(ValueError):
        ScanLauncher(tmp_path).start({"target": target})


def test_launch_without_shell_and_single_flight(tmp_path, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr("Ben.interface.viewer.launcher.subprocess.Popen", spawn)
    launcher = ScanLauncher(tmp_path)
    result = launcher.start({"target": "http://example.com", "instruction": "read only"})
    assert result["running"]
    command = spawn.call_args.args[0]
    assert command[command.index("-t") + 1] == "http://example.com"
    assert "shell" not in spawn.call_args.kwargs
    with pytest.raises(RuntimeError):
        launcher.start({"target": "http://example.com"})


@pytest.mark.parametrize("target", ["117.72.199.2", "117.72.199.2:3000", "example.com/path"])
def test_bare_target_uses_http(tmp_path, monkeypatch, target):
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr("Ben.interface.viewer.launcher.subprocess.Popen", spawn)
    result = ScanLauncher(tmp_path).start({"target": target})
    assert result["target"] == "http://" + target
    command = spawn.call_args.args[0]
    assert command[command.index("-t") + 1] == "http://" + target


def test_scan_api_requires_session(tmp_path):
    import requests

    launcher = ScanLauncher(tmp_path)
    server, url, token = serve(tmp_path / "strix_runs/empty", open_browser=False, launcher=launcher)
    try:
        assert (
            requests.post(
                url + "/api/scans", json={"target": "http://example.com"}, timeout=5
            ).status_code
            == 403
        )
        session = requests.Session()
        assert session.get(url + "/?token=" + token, timeout=5).status_code == 200
        assert session.get(url + "/api/scans", timeout=5).json()["running"] is False
        assert (
            session.post(
                url + "/api/scans", json={"target": "file:///etc/passwd"}, timeout=5
            ).status_code
            == 400
        )
        assert (
            session.post(
                url + "/api/scans", json={}, headers={"Sec-Fetch-Site": "cross-site"}, timeout=5
            ).status_code
            == 403
        )
    finally:
        server.shutdown()
        server.server_close()


def test_pause_and_resume_running_scan(tmp_path, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr("Ben.interface.viewer.launcher.subprocess.Popen", Mock(return_value=process))
    launcher = ScanLauncher(tmp_path)
    launcher.start({"target": "http://example.com"})
    assert launcher.set_paused(True)["paused"] is True
    assert launcher.control_path.exists()
    assert launcher.set_paused(False)["paused"] is False
    assert not launcher.control_path.exists()
    process.poll.return_value = 0
    with pytest.raises(RuntimeError, match="没有正在运行"):
        launcher.set_paused(True)

def test_pause_resume_http_routes(tmp_path, monkeypatch):
    import requests

    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr("Ben.interface.viewer.launcher.subprocess.Popen", Mock(return_value=process))
    launcher = ScanLauncher(tmp_path)
    launcher.start({"target": "http://example.com"})
    server, url, token = serve(tmp_path / "strix_runs/empty", open_browser=False, launcher=launcher)
    try:
        session = requests.Session()
        session.get(url + "/?token=" + token, timeout=5).raise_for_status()
        for action, paused in [("pause", True), ("resume", False)]:
            response = session.post(url + "/api/scans/" + action, timeout=5)
            assert response.status_code == 202
            assert response.json()["paused"] is paused
            assert session.get(url + "/api/scans", timeout=5).json()["paused"] is paused
    finally:
        server.shutdown()
        server.server_close()
