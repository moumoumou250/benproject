"""``_copy_kb_evidence``：递归拷贝 + 开关关闭零 I/O（假 sandbox session）。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from Ben.core.runner import _copy_kb_evidence


class _ExecResult:
    def __init__(self, stdout: bytes, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = b""
        self.exit_code = exit_code

    def ok(self) -> bool:
        return self.exit_code == 0


class _FakeSandbox:
    """最小假 session：exec 回 find 列表，read 回内存文件。"""

    def __init__(self, tree: dict[str, bytes]) -> None:
        # relpath → bytes（相对 /workspace/.evidence）
        self.tree = dict(tree)
        self.exec_cmds: list[str] = []
        self.read_paths: list[str] = []

    async def exec(self, *command: str, shell: bool | list[str] = True, **_kw):
        cmd = " ".join(str(c) for c in command) if command else ""
        self.exec_cmds.append(cmd)
        # 模拟 find -type f 输出相对路径
        listing = "\n".join(sorted(self.tree)) + ("\n" if self.tree else "")
        return _ExecResult(listing.encode("utf-8"))

    async def read(self, path: Path, **_kw):
        rel = Path(path).as_posix()
        prefix = "/workspace/.evidence/"
        if not rel.startswith(prefix):
            raise FileNotFoundError(rel)
        key = rel[len(prefix) :]
        self.read_paths.append(key)
        if key not in self.tree:
            # 目录项：模拟旧 bug（IsADirectoryError）
            if any(k.startswith(key.rstrip("/") + "/") for k in self.tree):
                raise IsADirectoryError(rel)
            raise FileNotFoundError(rel)
        return io.BytesIO(self.tree[key])


@pytest.mark.asyncio
async def test_copy_kb_evidence_off_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BEN_KB_EVIDENCE", raising=False)
    sandbox = _FakeSandbox({"001.req": b"GET /x\n"})
    stats = await _copy_kb_evidence(sandbox, tmp_path / "run")
    assert stats["enabled"] is False
    assert sandbox.exec_cmds == []
    assert not (tmp_path / "run" / ".evidence").exists()


@pytest.mark.asyncio
async def test_copy_kb_evidence_flat_and_nested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEN_KB_EVIDENCE", "1")
    sandbox = _FakeSandbox(
        {
            "001_GET_health.req": b"GET /actuator/health HTTP/1.1\n",
            "001_GET_health.resp": b"HTTP/1.1 200 OK\n\n",
            "http/002_POST_routes.req": b"POST /actuator/gateway/routes HTTP/1.1\n",
            "http/002_POST_routes.resp": b"HTTP/1.1 201 Created\n\n",
        }
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    stats = await _copy_kb_evidence(sandbox, run_dir)
    dest = run_dir / ".evidence"
    assert stats["copied"] == 4
    assert (dest / "001_GET_health.req").is_file()
    assert (dest / "http" / "002_POST_routes.req").is_file()
    assert (dest / "http" / "002_POST_routes.resp").read_bytes().startswith(b"HTTP/1.1 201")
    assert "http/002_POST_routes.req" in sandbox.read_paths
    assert "http" not in sandbox.read_paths
    assert (run_dir / "kb_evidence.json").is_file()
    import json

    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["kb_evidence"]["copied"] == 4


@pytest.mark.asyncio
async def test_copy_kb_evidence_rejects_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BEN_KB_EVIDENCE", "1")
    sandbox = _FakeSandbox({"../escape.req": b"GET /x\n", "ok.req": b"GET /y\n"})
    run_dir = tmp_path / "run"
    await _copy_kb_evidence(sandbox, run_dir)
    dest = run_dir / ".evidence"
    assert (dest / "ok.req").is_file()
    assert not (tmp_path / "escape.req").exists()
    assert list(dest.rglob("escape.req")) == []
