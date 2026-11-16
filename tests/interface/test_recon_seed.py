"""recon/scout 种子桥接：加载、去重、自动发现、注入 prepare_run。"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from Ben.interface.cli.seed import (
    apply_seed_to_prepared,
    find_auto_seed,
    load_seed_path,
    render_seed_brief,
    resolve_seed_for_run,
)


def _write_scout(dir_path: Path, *, host: str = "app.example.com") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "target": f"http://{host}/",
        "host": host,
        "verdict": {"difficulty": "medium", "why": "auth wall"},
        "facts": [
            {"kind": "host", "title": host, "data": {"alive": True}, "source": "dns"},
            {
                "kind": "service",
                "title": "443/tcp",
                "data": {"port": 443, "service": "https", "product": "nginx"},
                "source": "probe",
            },
            {
                "kind": "service",
                "title": "443/tcp",
                "data": {"port": 443, "service": "https", "product": "nginx"},
                "source": "probe-dup",
            },
            {
                "kind": "surface",
                "title": "入口面",
                "data": {"entries": [{"type": "path", "where": "/login"}]},
                "source": "http",
            },
        ],
        "suggestions": [{"title": "auth", "why": "login form"}],
        "notes": ["waf likely"],
    }
    path = dir_path / "scout.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_seed_dedupes_identical_facts(tmp_path: Path) -> None:
    path = _write_scout(tmp_path / "app.example.com_20261116_scout")
    bundle = load_seed_path(path)
    assert bundle.kind == "scout"
    assert bundle.host == "app.example.com"
    assert bundle.verdict and bundle.verdict["difficulty"] == "medium"
    # 两条相同 service 应去重为 1
    services = [f for f in bundle.facts if f.kind == "service"]
    assert len(services) == 1
    assert len(bundle.facts) == 3


def test_load_seed_from_directory_prefers_scout(tmp_path: Path) -> None:
    d = tmp_path / "host_stamp_scout"
    scout = _write_scout(d)
    (d / "recon.json").write_text(json.dumps({"facts": []}), encoding="utf-8")
    bundle = load_seed_path(d)
    assert bundle.source_path == scout.resolve()


def test_find_auto_seed_picks_newest_matching_scout(tmp_path: Path) -> None:
    older = tmp_path / "app.example.com_20260101_120000_scout"
    newer = tmp_path / "app.example.com_20261116_120000_scout"
    _write_scout(older)
    _write_scout(newer)
    # 保证 newer 更新
    older.joinpath("scout.json").touch()
    import os
    import time

    os.utime(newer, (time.time() + 10, time.time() + 10))
    found = find_auto_seed(["app.example.com"], recon_root=tmp_path)
    assert found is not None
    assert found.parent.name.endswith("_scout")
    assert "20261116" in found.parent.name


def test_resolve_seed_respects_no_seed(tmp_path: Path) -> None:
    path = _write_scout(tmp_path / "app.example.com_x_scout")
    assert (
        resolve_seed_for_run(
            targets_info=[
                {
                    "type": "web_application",
                    "original": "http://app.example.com/",
                    "details": {"target_url": "http://app.example.com/"},
                }
            ],
            seed_arg=None,
            no_seed=True,
            recon_root=tmp_path,
        )
        is None
    )
    bundle = resolve_seed_for_run(
        targets_info=[],
        seed_arg=str(path),
        no_seed=False,
    )
    assert bundle is not None
    assert bundle.host == "app.example.com"


def test_apply_seed_injects_brief_and_workspace_file(tmp_path: Path) -> None:
    path = _write_scout(tmp_path / "app.example.com_x_scout")
    bundle = load_seed_path(path)
    instruction, files = apply_seed_to_prepared(
        instruction="Focus on IDOR.",
        workspace_files=[],
        bundle=bundle,
    )
    assert instruction.startswith("## Pre-seeded reconnaissance")
    assert "Focus on IDOR." in instruction
    assert "scout verdict: **medium**" in instruction
    assert any(f["workspace_path"] == "/workspace/preseed/facts.json" for f in files)
    payload = json.loads(files[0]["content"].decode("utf-8"))
    assert payload["host"] == "app.example.com"
    assert len(payload["facts"]) == 3


def test_render_seed_brief_mentions_preseed_path(tmp_path: Path) -> None:
    path = _write_scout(tmp_path / "h_scout")
    brief = render_seed_brief(load_seed_path(path))
    assert "/workspace/preseed/facts.json" in brief
    assert "[host]" in brief


def test_prepare_run_applies_explicit_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from Ben.interface.cli.scan_setup import prepare_run

    seed = _write_scout(tmp_path / "app.example.com_x_scout")
    # 避免真去解析 diff / clone
    monkeypatch.setattr(
        "Ben.interface.shared.targets.generate_run_name",
        lambda _t: "run_seed_test",
    )
    monkeypatch.setattr(
        "Ben.interface.shared.targets.resolve_diff_scope_context",
        lambda **_k: type(
            "D",
            (),
            {"instruction_block": "", "metadata": {"active": False}},
        )(),
    )
    monkeypatch.setattr(
        "Ben.interface.shared.targets.read_workspace_files",
        lambda _files: [],
    )
    monkeypatch.setattr(
        "Ben.interface.shared.targets.collect_local_sources",
        lambda _t: [],
    )
    monkeypatch.setattr(
        "Ben.interface.shared.targets.stage_api_specs",
        lambda *_a, **_k: [],
    )

    args = Namespace(
        resume=None,
        targets_info=[
            {
                "type": "web_application",
                "original": "http://app.example.com/",
                "details": {"target_url": "http://app.example.com/"},
            }
        ],
        instruction="extra",
        workspace_files=[],
        workspace_mount=None,
        scope_mode="auto",
        diff_base=None,
        seed=str(seed),
        no_seed=False,
        local_sources=[],
    )
    prepared = prepare_run(args)
    assert prepared["instruction"] is not None
    assert "## Pre-seeded reconnaissance" in prepared["instruction"]
    assert "extra" in prepared["instruction"]
    assert any(
        entry.get("workspace_path") == "/workspace/preseed/facts.json"
        for entry in prepared["workspace_files"]
    )
