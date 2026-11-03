"""Shell completion candidates and scripts.

Ben 的补全面比上游窄：没有 ``cloud`` 子命令树（托管云路径已剔除），
只剩根命令 ``view`` / ``completions`` / ``completion``、``view`` 的
flags，以及 ``./strix_runs`` 下的 run 名称。上游针对 ``cloud ...`` 的
用例随该子树一并移除，改为覆盖 Ben 实际暴露的补全面。
"""

from __future__ import annotations

import json
from typing import Any

from Ben.interface.cli.completions import completion_candidates, run_completions


def _write_run(base: Any, name: str, *, record: bool = True) -> None:
    run_dir = base / "strix_runs" / name
    run_dir.mkdir(parents=True)
    if record:
        (run_dir / "run.json").write_text(json.dumps({"run_name": name}), encoding="utf-8")


def test_root_completion_candidates() -> None:
    assert completion_candidates(["vi"]) == ["view"]
    assert "completions" in completion_candidates([""])
    assert "completion" in completion_candidates(["complet"])


def test_view_flag_candidates() -> None:
    candidates = completion_candidates(["view", "--"])
    assert {"--port", "--host", "--no-open", "--help"} <= set(candidates)


def test_view_run_name_candidates(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    _write_run(tmp_path, "ben_20260101_aaaa")
    _write_run(tmp_path, "ben_20260102_bbbb")
    # 没有 run.json 的目录不是可恢复 run，不参与补全。
    (tmp_path / "strix_runs" / "not-a-run").mkdir(parents=True)

    assert completion_candidates(["view", ""]) == [
        "ben_20260101_aaaa",
        "ben_20260102_bbbb",
    ]
    assert completion_candidates(["view", "ben_20260102"]) == ["ben_20260102_bbbb"]


def test_view_value_flag_suppresses_further_candidates(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    _write_run(tmp_path, "ben_20260101_aaaa")

    # --port 的值是端口号，既不是 flag 也不是 run 名。
    assert completion_candidates(["view", "--port", ""]) == []
    assert completion_candidates(["view", "--host", ""]) == []


def test_filesystem_completion_omits_terminal_control_names(
    tmp_path: Any, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_run(tmp_path, "safe_run")
    # Windows 不允许文件名含换行/转义序列，因此直接替换 run 名来源，
    # 单独验证候选过滤这一层（同名目录在 POSIX 上才建得出来）。
    from Ben.interface.cli import completions as completions_module

    monkeypatch.setattr(
        completions_module,
        "_run_names",
        lambda: ["safe_run", "unsafe\nname", "unsafe\x1b]52;c;payload\x07name"],
    )

    assert completion_candidates(["view", ""]) == ["safe_run"]
    assert run_completions(["--candidates", "view", ""]) == 0
    assert capsys.readouterr().out == "safe_run\n"


def test_unknown_subcommand_yields_no_candidates() -> None:
    assert completion_candidates(["cloud", "work"]) == []


def test_completion_scripts_cover_supported_shells(capsys: Any) -> None:
    for shell in ("zsh", "bash", "fish"):
        assert run_completions([shell]) == 0
        output = capsys.readouterr().out
        assert "completions --candidates" in output


def test_bash_completion_preserves_candidates_with_spaces(capsys: Any) -> None:
    assert run_completions(["bash"]) == 0
    output = capsys.readouterr().out
    assert 'COMPREPLY=("${candidates[@]}")' in output
    assert "while IFS= read -r candidate" in output
    assert "mapfile" not in output


def test_completion_rejects_unknown_shell(capsys: Any) -> None:
    assert run_completions(["powershell\x1b]52;c;payload\x07"]) == 2
    error = capsys.readouterr().err
    assert "Choose zsh, bash, or fish" in error
    assert "\x1b" not in error
    assert "\\x1b" in error


def test_completion_help_lists_supported_shells(capsys: Any) -> None:
    assert run_completions([]) == 0
    assert "zsh|bash|fish" in capsys.readouterr().out
