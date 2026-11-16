"""``ben vuln``：聚焦单类漏洞的 CLI。

只测"解析 + 装配 + 分发"这三段，不启动 Docker、不调用模型：

- 类型解析（别名、逗号分隔、未知名给建议、技能上限）；
- 聚焦指令真的拼进 instruction（模型看到的是指令，不是文件路径）；
- args 对象的字段与 ``parse_arguments`` 同构——这是复用 ``_run_task`` 的唯一契约，
  字段漏一个，``prepare_run``/``run_cli`` 就会在 getattr 上悄悄走默认值。

端到端（真跑一次扫描）不在单测范围：那需要 Docker + API Key，按 README §七 的
自检顺序人工验。
"""

from __future__ import annotations

import argparse

import pytest

from Ben.interface.cli import vuln as vuln_cli
from Ben.interface.cli.vuln import (
    VulnTypeError,
    build_parser,
    resolve_vulnerability_skills,
    run_vuln,
)


def test_alias_resolves_to_qualified_skill():
    assert resolve_vulnerability_skills(["sqli"]) == ["vulnerabilities/sql-injection"]
    assert resolve_vulnerability_skills(["SQL_Injection"]) == ["vulnerabilities/sql-injection"]
    assert resolve_vulnerability_skills(["lfi"]) == ["vulnerabilities/path-traversal-lfi-rfi"]
    assert resolve_vulnerability_skills(["jwt"]) == ["vulnerabilities/authentication-jwt"]


def test_comma_separated_types_dedupe():
    skills = resolve_vulnerability_skills(["xss, csrf", "xss"])
    assert skills == ["vulnerabilities/xss", "vulnerabilities/csrf"]


def test_extra_skills_appended_without_duplicates():
    skills = resolve_vulnerability_skills(["xss"], ["tooling/nuclei", "vulnerabilities/xss"])
    assert skills == ["vulnerabilities/xss", "tooling/nuclei"]


def test_unknown_type_suggests_close_matches():
    with pytest.raises(VulnTypeError) as excinfo:
        resolve_vulnerability_skills(["sqlii"])
    message = str(excinfo.value)
    assert "sqlii" in message
    assert "sql-injection" in message
    assert "vuln --list" in message


def test_skill_count_is_capped_at_five():
    with pytest.raises(VulnTypeError) as excinfo:
        resolve_vulnerability_skills(["xss,csrf,ssrf,idor,ssti,xxe"])
    assert "技能过多" in str(excinfo.value)


def test_list_does_not_need_config_or_docker(capsys):
    assert run_vuln(["--list"]) == 0
    out = capsys.readouterr().out
    assert "sql-injection" in out
    # 别名要露出来，否则用户猜不到 sqli 能写
    assert "sqli" in out


def test_focus_block_is_prepended_to_instruction():
    captured: dict[str, argparse.Namespace] = {}

    def fake_run_task(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 0

    code = run_vuln(
        ["sql-injection", "-t", "http://127.0.0.1:3000/", "--instruction", "只测登录接口"],
        run_task=fake_run_task,
    )
    assert code == 0
    instruction = captured["args"].instruction
    assert instruction.startswith("<focused_scan")
    assert "vulnerabilities/sql-injection" in instruction
    assert instruction.rstrip().endswith("只测登录接口")


def test_run_args_match_parse_arguments_contract():
    """字段与 ``parse_arguments()`` 对齐，``prepare_run`` 才能原样复用。"""
    parser = build_parser()
    ns = parser.parse_args(["sqli", "-t", "http://127.0.0.1:3000/"])
    args = vuln_cli._build_run_args(ns, parser, "instruction")

    expected = {
        "target",
        "targets",
        "target_list",
        "instruction",
        "instruction_file",
        "scan_mode",
        "scope_mode",
        "diff_base",
        "resume",
        "interactive",
        "non_interactive",
        "workspace_file",
        "workspace_files",
        "max_budget_usd",
        "max_turns",
        "fail_on",
        "skip_preflight",
        "preflight_timeout",
        "keep_container",
        "verbose",
        "targets_info",
        "local_sources",
        "diff_scope",
        "user_explicit_instruction",
        "workspace_mount",
        "run_name",
    }
    missing = expected - set(vars(args))
    assert not missing, f"args 缺少字段：{sorted(missing)}"
    assert args.non_interactive is True  # 子命令不启 TUI
    assert args.scan_mode == "quick"  # 聚焦模式默认 quick
    assert args.targets_info and args.targets_info[0]["original"] == "http://127.0.0.1:3000/"


def test_prepare_run_accepts_the_namespace():
    """真把 args 交给 prepare_run：不建目录、不联网，只验证它能跑通。"""
    from Ben.interface.cli.scan_setup import prepare_run

    parser = build_parser()
    ns = parser.parse_args(["sqli", "-t", "http://127.0.0.1:3000/"])
    args = vuln_cli._build_run_args(ns, parser, "instruction")
    prepared = prepare_run(args)
    assert prepared["run_name"]
    assert prepared["instruction"] == "instruction"
    assert prepared["targets_info"]


def test_dry_run_does_not_call_run_task(capsys):
    def exploding_run_task(args):  # noqa: ANN001, ARG001
        raise AssertionError("dry-run 不应进入扫描链路")

    assert run_vuln(["sqli", "-t", "http://127.0.0.1:3000/", "--dry-run"], run_task=exploding_run_task) == 0
    assert "[DRY-RUN]" in capsys.readouterr().out


def test_missing_type_exits_2():
    with pytest.raises(SystemExit) as excinfo:
        run_vuln([])
    assert excinfo.value.code == 2
