"""命令行参数解析（对应 strix 的 interface/cli_args.py）。

多目标入口：
  - 位置参数 ``target``（向后兼容，单目标）与 ``-t/--target``（可重复）等价合并；
  - ``--target-list`` 从文件读取目标（每行一个，# 为注释）；
  - parse 阶段经 Ben.interface.shared.targets.build_targets_info 完成目标分类、
    本地目录挂载校验、workspace 子目录分配与 localhost 改写；
    结果挂在 ``args.targets_info``（空列表 = 未指定，由 runner 回退白名单首项）。

断点续扫（对齐 strix load_resume_state）：
  - ``-r/--resume RUN_NAME`` 从 strix_runs/<run_name>/run.json 恢复
    targets_info/scan_mode/instruction/diff_scope/local_sources，
    并校验 .state/agents.json 存在（root/子 agent 历史在 .state/agents.db）；
  - 与 --target/--target-list 互斥；
  - 裸 ``--resume``：交互模式下由 TUI 选择器挑 run；headless 下列出可恢复
    runs 并报错退出，提示带名字重试。

运行控制（对齐 strix）：
  - ``--max-budget`` / ``--max-turns``：成本与轮次上限；
  - ``--fail-on``：按最高漏洞等级决定 CI 退出码；
  - ``--workspace-file PATH[:DEST]``：扫描前把本机文件放进 /workspace；
  - ``--mcp-config`` / ``--mcp-server`` / ``--mcp-exclude``：运行级 MCP 选择；
  - ``-n/--non-interactive``：关闭 TUI，跑完即退。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


class ResumeError(ValueError):
    """A prior run cannot be resumed as recorded; the message names why."""


def terminal_attached() -> bool:
    """本进程是否连着终端（决定 TUI 还是 headless）。

    实现在 :mod:`Ben.interface.terminal.text`；这里保留模块级别名，让
    ``--fail-on`` 的 headless 校验可被单测替换（对齐 strix 的同名函数）。
    """
    from Ben.interface.terminal.text import terminal_attached as _attached

    return _attached()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BenProject 多 Agent 漏洞测试工具（DeepSeek + Docker）"
    )

    parser.add_argument(
        "target",
        nargs="?",
        default="",
        help="授权目标（URL/域名/IP/本地目录/git 仓库），等价于 -t/--target",
    )
    parser.add_argument(
        "-t",
        "--target",
        dest="targets",
        action="append",
        metavar="TARGET",
        help="测试目标：URL、git 仓库、本地目录、域名或 IP。可重复指定实现多目标扫描。",
    )
    parser.add_argument(
        "--target-list",
        action="append",
        metavar="PATH",
        help="目标清单文件：每行一个目标，忽略空行与 # 注释。可与 -t/--target 混用。",
    )
    parser.add_argument(
        "--instruction",
        help="自定义测试指令（如 '重点测试 IDOR 与 XSS'、测试凭据等）。",
    )
    parser.add_argument(
        "--instruction-file",
        metavar="PATH",
        help="从文件读取自定义指令（较长或复杂的指令用文件）。与 --instruction 互斥。",
    )
    parser.add_argument(
        "-m",
        "--scan-mode",
        choices=["quick", "standard", "deep"],
        default="deep",
        help="扫描模式：quick（CI 快速检查）/ standard（常规）/ deep（深度，默认）。",
    )
    parser.add_argument(
        "--scope-mode",
        choices=["auto", "diff", "full"],
        default="auto",
        help="代码目标范围：auto（CI/PR 环境自动启用 diff 范围）/ diff（强制变更文件范围）"
        "/ full（关闭 diff 范围）。",
    )
    parser.add_argument(
        "--diff-base",
        metavar="REF",
        help="diff 范围的对照分支或提交（如 origin/main），默认取仓库默认分支。",
    )
    parser.add_argument(
        "-r",
        "--resume",
        type=str,
        nargs="?",
        const="",
        metavar="RUN_NAME",
        help=(
            "按 run 名称恢复既往扫描（./strix_runs/ 下的目录名），接续 root 与"
            "未终结子 agent 的完整 LLM 历史与图拓扑。与 --target/--target-list"
            " 互斥；不带名称时列出可恢复的 runs 并退出。"
        ),
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="交互模式：agent 可调用 wait_for_user 交还控制权，挂起后等待消息唤醒。",
    )
    parser.add_argument(
        "-n",
        "--non-interactive",
        action="store_true",
        help="非交互模式：不启动 TUI，完成后直接退出（CI/脚本用）。",
    )
    parser.add_argument(
        "--workspace-file",
        action="append",
        metavar="PATH[:DEST]",
        help="扫描前把本机文件放进沙箱 /workspace（可重复）。DEST 省略时用文件名，"
        "例如 --workspace-file ./wordlist.txt:lists/wordlist.txt。",
    )
    parser.add_argument(
        "--max-budget",
        "--max-budget-usd",
        dest="max_budget_usd",
        type=float,
        metavar="USD",
        help="本次扫描的 LLM 成本上限（美元）；达到后按预算策略停止或暂停。",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        metavar="N",
        help="每个 agent 的最大轮数（默认取 settings.max_agent_turns）。",
    )
    parser.add_argument(
        "--fail-on",
        dest="fail_on",
        type=str.lower,
        choices=["critical", "high", "medium", "low", "info"],
        metavar="SEVERITY",
        help="发现该等级及以上的漏洞时以退出码 1 结束（CI 门禁）。",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="使用指定 cli-config.json，而不是 ~/.ben/cli-config.json。",
    )
    parser.add_argument(
        "--mcp-config",
        metavar="PATH",
        help="使用指定 MCP servers JSON，而不是 ~/.ben/mcp-servers.json。",
    )
    parser.add_argument(
        "--mcp-server",
        dest="mcp_server",
        action="append",
        metavar="NAME",
        help="只启用指定名称的 MCP 连接（可重复）。",
    )
    parser.add_argument(
        "--mcp-exclude",
        dest="mcp_exclude",
        action="append",
        metavar="NAME",
        help="跳过指定名称的 MCP 连接（可重复）。",
    )
    parser.add_argument("--selftest-docker", action="store_true", help="仅验证 Docker 执行器")
    parser.add_argument(
        "--selftest-llm",
        action="store_true",
        help="验证模型节点连通：总节点 LLM_MODEL + 子节点 BEN_FLASH_MODEL（未配置则跳过）",
    )
    parser.add_argument("--selftest-tools", action="store_true", help="仅验证工具层真实执行")
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="跳过开跑前的目标可达性预检（默认会先探测，不可达就拒绝启动）",
    )
    parser.add_argument(
        "--preflight-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="预检单次探测超时（默认 5 秒）",
    )
    parser.add_argument("--keep-container", action="store_true", help="任务结束保留执行容器")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")

    args = parser.parse_args()

    # parse 阶段解析出的运行输入一律挂在 args 上（对齐 strix 的
    # startup-resolved state 约定），resume 分支会整体覆盖它们。
    args.targets_info = []
    args.local_sources = []
    args.diff_scope = {"active": False}
    args.workspace_files = []
    args.needs_setup = False

    if args.max_turns is not None and args.max_turns < 1:
        parser.error("--max-turns must be a positive integer")
    if args.max_budget_usd is not None and args.max_budget_usd <= 0:
        parser.error("--max-budget must be greater than 0")
    if getattr(args, "preflight_timeout", 5.0) <= 0:
        parser.error("--preflight-timeout must be greater than 0")

    # --fail-on 只在 headless 有意义：没有终端时 main() 本来就走 headless，
    # 只有 TUI 真要打开时该参数才是错配（对齐 strix）。
    if args.fail_on and not args.non_interactive and terminal_attached():
        parser.error("--fail-on only applies to headless runs; add -n/--non-interactive.")

    # MCP 运行级选择：--mcp-config 覆盖 loader 的默认路径，--mcp-server /
    # --mcp-exclude 写入选集环境变量（loader 在 runner 构造时读取）。
    if args.mcp_config:
        mcp_config_path = Path(args.mcp_config).expanduser()
        if not mcp_config_path.is_file():
            parser.error(f"--mcp-config file not found: {args.mcp_config}")
        os.environ["BEN_MCP_CONFIG"] = str(mcp_config_path)
    if args.mcp_server:
        os.environ["BEN_MCP_ONLY"] = ",".join(args.mcp_server)
    if args.mcp_exclude:
        os.environ["BEN_MCP_EXCLUDE"] = ",".join(args.mcp_exclude)

    # 冲突校验（对齐 strix）：--instruction 与 --instruction-file 互斥。
    if args.instruction and args.instruction_file:
        parser.error(
            "Cannot specify both --instruction and --instruction-file. Use one or the other."
        )
    if args.instruction_file:
        instruction_path = Path(args.instruction_file)
        try:
            args.instruction = instruction_path.read_text(encoding="utf-8").strip()
            if not args.instruction:
                parser.error(f"Instruction file '{instruction_path}' is empty")
        except OSError as e:
            parser.error(f"Failed to read instruction file '{instruction_path}': {e}")

    # 用户本次显式给出的 instruction（须在 load_resume_state 用 run.json 回填
    # args.instruction 之前捕获；resume 时它作为 resume_instruction 注入 root
    # 会话——对齐 strix user_explicit_instruction）。
    args.user_explicit_instruction = args.instruction if args.resume is not None else None

    if args.resume is not None:
        if args.target or args.targets or args.target_list:
            parser.error(
                "Cannot combine --resume with --target/--target-list. "
                "--resume picks up where the prior run left off, including the "
                "original target list."
            )
        if not args.resume.strip():
            # 裸 --resume：main() 先开既往 runs 的内联选择器再启动；
            # headless 没人可选，于是列出 runs 并报错。
            args.resume = None
            args.resume_picker = True
            if args.non_interactive:
                parser.error(
                    resume_run_list_message("--resume needs a run name in headless mode.")
                )
            from Ben.core.paths import RUNS_DIR_NAME
            from Ben.report.runs import list_run_summaries

            if not list_run_summaries():
                parser.error(f"--resume: no runs in ./{RUNS_DIR_NAME} to resume")
            return args
        try:
            load_resume_state(args)
        except ResumeError as exc:
            parser.error(str(exc))
        return args

    if args.workspace_file:
        from Ben.interface.shared.targets import resolve_workspace_files

        try:
            args.workspace_files = resolve_workspace_files(args.workspace_file)
        except ValueError as e:
            parser.error(str(e))

    # 合并位置参数与 -t/--target/--target-list，得到统一的目标字符串列表。
    targets: list[str] = []
    if args.target:
        targets.append(args.target)
    targets.extend(args.targets or [])

    from Ben.interface.shared.targets import build_targets_info, read_target_list_file

    for target_list_path in args.target_list or []:
        try:
            targets.extend(read_target_list_file(target_list_path))
        except ValueError as e:
            parser.error(str(e))

    # parse 阶段完成目标分类/校验；空目标列表留给 runner 回退白名单首项。
    if targets:
        try:
            args.targets_info = build_targets_info(targets)
        except ValueError as e:
            parser.error(str(e))
    else:
        # 交互式且未给目标：进入 TUI 起始页，由用户在界面里填写目标或指令
        # （对齐 strix）。headless 保留原有“回退白名单首项”行为。
        if not args.non_interactive and terminal_attached():
            args.needs_setup = True

    return args


def resume_run_list_message(lead: str) -> str:
    """``lead`` 加上 ``--resume <name>`` 可接受的 runs 列表（对齐 strix）。"""
    from Ben.core.paths import RUNS_DIR_NAME
    from Ben.report.runs import list_run_summaries

    runs = list_run_summaries()
    if not runs:
        return f"{lead} There are no runs in ./{RUNS_DIR_NAME}."
    name_width = max(len(run.run_name) for run in runs)
    status_width = max(len(run.status) for run in runs)
    lines = [f"{lead} Runs in ./{RUNS_DIR_NAME}:"]
    lines.extend(
        f"  {run.run_name:<{name_width}}  {run.status:<{status_width}}  "
        f"{run.started_at[:19]:<19}  {run.target}".rstrip()
        for run in runs
    )
    return "\n".join(lines)


def load_resume_state(args: argparse.Namespace) -> None:
    """从既往 run 的 run.json 恢复 ``args.targets_info`` 等运行输入。

    对齐 strix load_resume_state：run 缺失、run.json 不可读、工作目录已删、
    工作区文件路径被篡改或首个 agent 快照未到时抛 :class:`ResumeError`。
    """
    from Ben.core.paths import run_dir_for, run_record_path, runtime_state_dir
    from Ben.interface.cli.scan_setup import attach_workspace_mount
    from Ben.interface.shared.targets import (
        check_mountable_dir,
        collect_local_sources,
        resolve_workspace_files,
    )
    from Ben.report.writer import read_run_record

    run_dir = run_dir_for(args.resume)
    state_path = run_record_path(run_dir)
    if not state_path.exists():
        raise ResumeError(
            f"--resume {args.resume}: no such run "
            f"(missing {state_path}; remove --resume for a fresh start)"
        )
    try:
        state = read_run_record(run_dir)
    except (RuntimeError, TypeError) as exc:
        raise ResumeError(f"--resume {args.resume}: run.json unreadable: {exc}") from exc

    args.targets_info = state.get("targets_info") or []
    # 目标全失的 run 靠 instruction 驱动：只要挂过工作目录，或留下了
    # instruction，就足以续跑（对齐 strix）。
    workspace_mount = state.get("workspace_mount") or None
    if not args.targets_info and not workspace_mount and not state.get("instruction"):
        raise ResumeError(f"--resume {args.resume}: run.json has no targets_info")

    for target in args.targets_info:
        if not isinstance(target, dict):
            continue
        details = target.get("details") or {}
        if target.get("type") == "local_code" and details.get("target_path"):
            try:
                check_mountable_dir(Path(details["target_path"]).expanduser())
            except ValueError as exc:
                raise ResumeError(f"--resume {args.resume}: {exc}") from exc
            continue
        if target.get("type") != "repository":
            continue
        cloned = details.get("cloned_repo_path")
        if not cloned:
            continue
        if not Path(cloned).expanduser().exists():
            raise ResumeError(
                f"--resume {args.resume}: cloned repo at {cloned} is missing. "
                f"It was deleted between runs. Restore the directory before resuming."
            )

    if args.instruction is None:
        args.instruction = state.get("instruction")
    args.local_sources = collect_local_sources(args.targets_info)
    # 重新挂回 run 启动时用的工作目录。用户此前已确认过该目录，因此不再走
    # 目标挂载的授权校验，只需确认它还在。
    args.workspace_mount = workspace_mount

    # 重新放置 run 启动时注入的工作区文件（本次 resume 自带 --workspace-file
    # 时以本次为准）。持久化记录同样按新 flag 重新校验，避免手改 run.json
    # 把文件放到工作区之外；两次运行之间被删掉的文件直接丢弃（它只是上下文，
    # 不构成扫描范围）。
    if not getattr(args, "workspace_files", None):
        restored = [
            f"{source_path}:{workspace_path}"
            for workspace_file in state.get("workspace_files") or []
            if isinstance(workspace_file, dict)
            and (source_path := Path(str(workspace_file.get("source_path") or ""))).is_file()
            and (workspace_path := str(workspace_file.get("workspace_path") or ""))
        ]
        try:
            args.workspace_files = resolve_workspace_files(restored)
        except ValueError as error:
            raise ResumeError(
                f"--resume {args.resume}: invalid workspace file: {error}"
            ) from error
    if workspace_mount:
        if not Path(workspace_mount).expanduser().is_dir():
            raise ResumeError(
                f"--resume {args.resume}: the working directory {workspace_mount} "
                f"is missing. Restore it before resuming, or start a fresh run."
            )
        attach_workspace_mount(args)
    if state.get("diff_scope"):
        args.diff_scope = state.get("diff_scope")
    persisted_scan_mode = state.get("scan_mode")
    if persisted_scan_mode and args.scan_mode == "deep":
        args.scan_mode = persisted_scan_mode
    agents_path = runtime_state_dir(run_dir) / "agents.json"
    if not agents_path.exists():
        raise ResumeError(
            f"--resume {args.resume}: missing {agents_path}. The run was "
            f"persisted but never reached its first agent snapshot — "
            f"该 run 未到首个快照，无可恢复状态。"
            f"去掉 --resume 可用同样的目标重新开始。"
        )


# 兼容别名：早期代码以 ``parse_args`` 引用同一入口（上游名为 parse_arguments）。
parse_args = parse_arguments
