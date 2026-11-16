"""BenProject 程序入口。

用法：
  # 正常任务（URL 必须写在 .env 的 ALLOWED_TARGETS 里）
  .venv/Scripts/python.exe main.py http://target.example:8080/

  # 多目标 / 白盒（本地源码 + 部署实例）
  .venv/Scripts/python.exe main.py -t ./my-project -t https://staging.example.com
  .venv/Scripts/python.exe main.py --target-list ./targets.txt --scan-mode quick

  # diff 范围（PR 变更文件优先）
  .venv/Scripts/python.exe main.py -t ./my-project --scope-mode diff --diff-base origin/main

  # 断点续扫（按 run 名称恢复 strix_runs/ 下的既往扫描）
  .venv/Scripts/python.exe main.py --resume ben_20261102_101530_ab12cd34
  .venv/Scripts/python.exe main.py --resume ben_20261102_101530_ab12cd34 --instruction "换个方向：重点测 IDOR"

  # 交互模式（agent 可 wait_for_user 交还控制权）
  .venv/Scripts/python.exe main.py -t https://staging.example.com --interactive

  # 目标预检（不启动扫描、不消耗模型额度；检查授权范围与可达性）
  .venv/Scripts/python.exe main.py preflight http://target.example:8080/
  .venv/Scripts/python.exe main.py preflight --target-list ./targets.txt --json

  # 只做信息收集（只读 GET，不调模型、不建 run；产物落 recon/<host>_<时间戳>/）
  .venv/Scripts/python.exe main.py recon http://target.example:8080/
  .venv/Scripts/python.exe main.py recon https://staging.example.com --json --no-write

  # 侦察兵（时间盒 + 端口/版本/入口面 + easy|medium|hard 裁定；可导出 sapper fact）
  .venv/Scripts/python.exe main.py scout http://target.example:8080/ --minutes 4
  .venv/Scripts/python.exe main.py scout http://target.example:8080/ --emit sapper

  # 聚焦单类漏洞（只加载对应技能，开一个子 agent，不做全量侦察）
  .venv/Scripts/python.exe main.py vuln --list
  .venv/Scripts/python.exe main.py vuln sqli -t http://target.example:8080/ --max-turns 8

  # 本地网页查看器（实时/已完成的 run，含 PDF 导出）
  .venv/Scripts/python.exe main.py view [run_name] [--port 8080] [--host 127.0.0.1]

  # Shell 补全（zsh/bash/fish）
  .venv/Scripts/python.exe main.py completions bash

  # 仅验证 Docker 执行器（不需要 API Key）
  .venv/Scripts/python.exe main.py --selftest-docker

  # 仅验证模型文本调用（需要 .env 中的 API Key）
  .venv/Scripts/python.exe main.py --selftest-llm

  # 仅验证工具层（不需要 API Key，会真实在容器内执行一次 HTTP 检查）
  .venv/Scripts/python.exe main.py --selftest-tools

  # 任务结束保留执行容器（默认自动删除）
  .venv/Scripts/python.exe main.py http://target.example:8080/ --keep-container

模块协作（运行时各模块职责）：
  main.py (入口)
    → Ben.config.settings      读取 .env，校验配置
    → Ben.core.runner          编排：SDK配置→Agent构建→Sandbox创建→Runner执行→报告
      → Ben.agents.prompt      Jinja2 渲染系统提示词
      → Ben.agents.factory     构建 Agent（@function_tool 工具，无 hosted tools）
      → Ben.tools.<family>.tool  load_skill/mcp 工具（shell 工具由 SDK Shell capability 生成）
      → SDK Runner.run_streamed  模型决策↔工具调用↔结果回传 多轮循环
      → SDK DockerSandboxClient  Docker 容器生命周期 + exec 命令执行（shell 工具经 capability 绑定 session）
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path

# Windows asyncio 兼容
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _force_utf8_streams() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 代码页把中文面板写成乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8")


if sys.platform == "win32":
    _force_utf8_streams()


# 确保项目根在 sys.path 中（从任意 CWD 运行都能导入 Ben 包）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── .env 优先级修复 ──────────────────────────────────────────────
# 外部进程可能注入 BEN_LLM 等环境变量，其优先级高于 .env 文件。
# 在此先从 .env 加载；对同一字段的多个别名（厂商中立名 + 历史名），
# 只要 .env 里出现了其中任意一个，就把它的值同步到该字段的全部别名，
# 保证 .env 配置永远赢过外部环境变量残留。
from dotenv import dotenv_values as _dotenv_values  # noqa: E402
from dotenv import load_dotenv as _load_dotenv  # noqa: E402

_dotenv_path = PROJECT_ROOT / ".env"
if _dotenv_path.exists():
    _load_dotenv(_dotenv_path, override=False)  # 不覆盖已有的 env var
    _env_file_vals = _dotenv_values(_dotenv_path)

    def _sync_aliases(aliases: list[str]) -> None:
        value = next((_env_file_vals[a] for a in aliases if _env_file_vals.get(a)), None)
        if value:
            for alias in aliases:
                os.environ[alias] = value

    _sync_aliases(["LLM_MODEL", "BEN_LLM", "DEEPSEEK_MODEL"])
    _sync_aliases(["LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"])
    _sync_aliases(["LLM_API_BASE", "OPENAI_BASE_URL", "DEEPSEEK_BASE_URL"])

    # 目标白名单只认 .env。进程里残留的 ALLOWED_TARGETS 不能覆盖文件。
    _targets_raw = _env_file_vals.get("ALLOWED_TARGETS")
    if _targets_raw is not None and str(_targets_raw).strip():
        os.environ["ALLOWED_TARGETS"] = str(_targets_raw).strip()
    else:
        os.environ.pop("ALLOWED_TARGETS", None)

from Ben.config import load_settings  # noqa: E402
from Ben.config.keypool import apply_to_env  # noqa: E402
from Ben.interface.cli.arguments import parse_arguments  # noqa: E402


def _apply_keypool_overrides() -> None:
    """Key 池 active 连接对 CLI 路径（扫描 / 自检）同样生效。

    Web 控制台切换写入 ~/.ben/keypool.json；本进程直接改写 os.environ，
    在 load_settings 之前调用，使 pydantic-settings 以进程环境优先读到
    池里的连接。无 active entry 时不改任何变量（.env 默认行为）。
    """
    try:
        apply_to_env()
    except Exception:  # noqa: BLE001 - 池文件损坏不该挡住启动，回退 .env
        logging.getLogger(__name__).warning("keypool 应用失败，使用 .env 默认配置", exc_info=True)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _selftest_docker() -> int:
    """Docker 执行器自检。"""
    from Ben.runtime.docker_executor import _selftest
    return _selftest()


def _selftest_llm() -> int:
    """模型文本连通验证：总节点（LLM_MODEL）+ 子节点（BEN_FLASH_MODEL）。

    模型分层下 root agent 用总节点做决策，子 agent 用子节点做量产——两个
    节点的 key/端点/厂商都可能不同，必须分别发一条最小真实请求验证，任一
    配置的节点不可用都不能直接开扫（否则要么 root 起不来，要么所有子 agent
    空转烧总节点额度）。子节点未配置时合法（所有 agent 共用总节点），跳过
    并说明，不算失败。
    """
    settings = load_settings(require_api_key=True)
    import asyncio

    from Ben.config import configure_sdk_model_defaults

    configure_sdk_model_defaults(settings)

    from agents import RunConfig, Runner
    from agents.agent import Agent
    from agents.items import MessageOutputItem
    from Ben.config.models import BenProvider, build_flash_model

    _INSTRUCTIONS = "你是连通性测试助手，用一句话回答。"
    _INPUT = "请回复：模型连接正常"

    async def _probe(agent: Agent, run_config: RunConfig) -> str:
        # 与正式运行同一路由（model_provider 挂 BenProvider，支持 厂商/模型
        # 前缀）；flash 子节点直接用 build_flash_model 的 Model 实例——
        # 那正是运行时子 agent 拿到的同一个对象（含流式守卫/turn guard）。
        stream = Runner.run_streamed(
            agent,
            input=_INPUT,
            run_config=run_config,
            max_turns=1,
        )
        async for _ in stream.stream_events():
            pass
        texts: list[str] = []
        for item in stream.new_items:
            if isinstance(item, MessageOutputItem):
                raw = getattr(item, "raw_item", None)
                if raw and getattr(raw, "content", None):
                    content = raw.content
                    if isinstance(content, str):
                        texts.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            t = getattr(part, "text", None) if hasattr(part, "text") else None
                            if t:
                                texts.append(t)
        return "\n".join(texts)

    flash_name = settings.llm.flash_model
    total_steps = 3 if flash_name else 2
    print(f"[1/{total_steps}] 配置 SDK（model={settings.llm.model} @ {settings.llm.base_url}）...")

    results: list[tuple[str, bool, str]] = []

    # ---- 总节点：root agent / 决策档 ----
    print(f"[2/{total_steps}] 总节点 LLM_MODEL = {settings.llm.model}")
    root_agent = Agent(
        name="connectivity-test",
        instructions=_INSTRUCTIONS,
        tools=[],
        model=settings.llm.model,
    )
    root_run_config = RunConfig(
        model=settings.llm.model,
        model_provider=BenProvider(),
        trace_include_sensitive_data=False,
    )
    try:
        reply = asyncio.run(_probe(root_agent, root_run_config))
        print(f"  模型回复：{reply}")
        print("  [OK] 总节点可用")
        results.append((f"总节点 LLM_MODEL={settings.llm.model}", True, ""))
    except Exception as exc:
        print(f"  [FAIL] {exc}")
        results.append((f"总节点 LLM_MODEL={settings.llm.model}", False, str(exc)))

    # ---- 子节点：flash 档（子 agent 量产）；未配置则跳过 ----
    if not flash_name:
        print("  子节点 BEN_FLASH_MODEL 未配置 —— 子 agent 将共用总节点模型（跳过检测，不算失败）")
    else:
        print(f"[3/{total_steps}] 子节点 BEN_FLASH_MODEL = {flash_name}")
        try:
            flash_model = build_flash_model(settings)
            flash_agent = Agent(
                name="connectivity-test-flash",
                instructions=_INSTRUCTIONS,
                tools=[],
                model=flash_model,
            )
            flash_run_config = RunConfig(
                model=None,  # flash Model 实例挂在 agent 上，SDK 直接使用
                model_provider=BenProvider(),
                trace_include_sensitive_data=False,
            )
            reply = asyncio.run(_probe(flash_agent, flash_run_config))
            print(f"  模型回复：{reply}")
            print("  [OK] 子节点可用")
            results.append((f"子节点 BEN_FLASH_MODEL={flash_name}", True, ""))
        except Exception as exc:
            print(f"  [FAIL] {exc}")
            results.append((f"子节点 BEN_FLASH_MODEL={flash_name}", False, str(exc)))

    print()
    for label, ok, _err in results:
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
    failed = [r for r in results if not r[1]]
    if failed:
        print("ALL FAIL - 存在不可用的模型节点，开扫前请先修复配置")
        return 1
    print("ALL OK - 已配置的模型节点全部可用")
    return 0


def _selftest_tools() -> int:
    """工具层验证：用 SDK sandbox 在容器内执行一次 curl 检查。"""
    settings = load_settings(require_api_key=False)
    from Ben.config.settings import require_allowed_targets

    try:
        target = require_allowed_targets(settings)[0]
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1

    import asyncio
    import docker as docker_sdk
    from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions

    async def _test() -> int:
        dc = docker_sdk.from_env()
        client = DockerSandboxClient(dc)
        opts = DockerSandboxClientOptions(image=settings.executor_image)
        session = await client.create(options=opts)

        async with session:
            print(f"对授权目标执行 curl 检查：{target}")
            cmd = [
                "curl", "-sS", "-m", str(int(settings.command_timeout)),
                "-D", "-", "-o", "/dev/null",
                "-w", "__BEN_META__%{http_code}", "--", target + "/",
            ]
            result = await session.exec(*cmd, timeout=settings.command_timeout + 5, shell=False)

            stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else str(result.stdout)
            if result.exit_code != 0:
                print(f"[FAIL] curl 执行失败（exit={result.exit_code}）")
                return 1
            lines = [line for line in stdout.splitlines() if line.strip()]
            meta = next((line for line in lines if line.startswith("__BEN_META__")), "")
            status_code = meta.removeprefix("__BEN_META__") or "?"
            header_names = [
                line.split(":", 1)[0].strip()
                for line in lines
                if ":" in line and not line.startswith(("HTTP/", "__BEN_META__"))
            ]
            print(f"状态码：{status_code}")
            print(f"响应头：{header_names[:5]}")
            print("ALL OK - 工具层真实执行正常")
            return 0

    try:
        return asyncio.run(_test())
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1


FAIL_ON_SEVERITIES = ("critical", "high", "medium", "low", "info")


def findings_fail_build(reports: list[dict], fail_on: str | None) -> bool:
    """Whether findings should fail the build under ``--fail-on``."""
    if not reports:
        return False
    if fail_on is None:
        return True
    threshold = FAIL_ON_SEVERITIES.index(fail_on)
    for report in reports:
        severity = str(report.get("severity") or "").strip().lower()
        if severity == "none":
            continue
        if severity not in FAIL_ON_SEVERITIES:
            return True
        if FAIL_ON_SEVERITIES.index(severity) <= threshold:
            return True
    return False


def _run_task(args) -> int:
    """完整 Agent 任务（SDK Runner + SDK Sandbox 驱动）。

    流程（对齐 strix prepare_run 的 Ben 子集）：
      args.targets_info（parse 阶段已分类/校验；resume 时已从 run.json 恢复）
      → repository 目标 clone 到本地临时目录（resume 跳过，复用既有克隆）
      → api_spec 目标 staging（只读放进 /workspace/api-specs）
      → collect_local_sources（沙箱 bind mount 清单）
      → --workspace-file 读取为 extra_files
      → resolve_diff_scope_context（--scope-mode/--diff-base）
      → diff instruction_block 前置拼入 instruction
      → 构造 AgentRunner 并运行
    """
    from Ben.interface.cli.environment import pull_docker_image, validate_environment

    validate_environment()
    pull_docker_image()

    # 目标闸门：目标不可达就别开跑（历史教训：117-72-199-2-3000 那次靶场没起，
    # 白烧 445 秒与百万 token）。本地目录/仓库/spec 不在这里判，交给 prepare_run。
    if not getattr(args, "skip_preflight", False):
        from Ben.interface.cli.target_check import check_run_targets

        blocked = check_run_targets(args)
        if blocked is not None:
            return blocked

    from Ben.interface.cli.scan_setup import PreparationError, prepare_run

    try:
        prepared = prepare_run(args)
    except PreparationError as exc:
        print(f"[FAIL] {exc}")
        return 1
    targets_info = prepared["targets_info"]
    run_name = prepared["run_name"]
    resume_run_name = prepared["resume_run_name"]

    if not targets_info:
        print("未指定目标，使用 .env 中 ALLOWED_TARGETS 的第一项（runner 内回退）")

    # headless 实时视图（Ben.interface.cli.runner）内部构造 AgentRunner：
    # - 复用同一份 ReportState，漏洞发现即打印面板（对齐 strix 的 run_cli）；
    # - --verbose 时关闭 Live，避免日志与刷新互相覆盖；
    # - 非 TTY（CI/重定向）时自动降级为纯文本输出。
    import asyncio

    from Ben.interface.cli.runner import run_cli

    live = not bool(getattr(args, "verbose", False)) and sys.stdout.isatty()
    result = asyncio.run(run_cli(args, prepared, live=live))

    from Ben.core.paths import run_dir_for
    from Ben.interface.cli.debug_expect import (
        EXIT_EXPECT_MISS,
        format_debug_summary,
        load_findings_json,
        match_expect,
        normalize_expect_patterns,
    )

    effective_run_name = resume_run_name or run_name
    expect_patterns = normalize_expect_patterns(getattr(args, "expect", None))
    want_summary = bool(getattr(args, "debug_summary", False)) or bool(expect_patterns)

    if result is None:
        print("[INFO] 本次运行未产出最终结果（预算耗尽或被中断），详见 strix_runs 目录。")
    else:
        print("\n" + "=" * 70)
        print(result.report_text)
        print("=" * 70)
        print(f"结束原因：{result.stopped_reason}；工具调用：{len(result.invocations)} 次")
        print(f"报告文件：{result.report_path}")
        print(f"轨迹文件：{result.trace_path}")

    findings: list = []
    if effective_run_name:
        findings = load_findings_json(run_dir_for(effective_run_name) / "vulnerabilities.json")
    elif result is not None:
        findings = load_findings_json(result.report_path.parent / "vulnerabilities.json")

    expect_ok: bool | None = None
    expect_missing: list[str] = []
    expect_hits: dict = {}
    if expect_patterns:
        expect_ok, expect_missing, expect_hits = match_expect(findings, expect_patterns)

    if want_summary and effective_run_name:
        print()
        print(
            format_debug_summary(
                run_name=effective_run_name,
                findings=findings,
                expect_patterns=expect_patterns,
                invocations=list(getattr(result, "invocations", None) or []),
                expect_ok=expect_ok,
                expect_missing=expect_missing,
                expect_hits=expect_hits,
            )
        )

    # expect 优先于 fail_on：调试「必须检出」时先报未命中。
    if expect_patterns and expect_ok is False:
        print(f"[FAIL] --expect 未全部命中：{', '.join(expect_missing)}；退出码 {EXIT_EXPECT_MISS}")
        return EXIT_EXPECT_MISS

    fail_on = getattr(args, "fail_on", None)
    if fail_on is not None and findings_fail_build(findings, fail_on):
        print(f"[FAIL] 存在 {fail_on} 及以上等级漏洞，退出码 2")
        return 2
    return 0


def _apply_runtime_env(args) -> None:
    """把运行级 MCP 选项写入环境变量（loader 在 runner 构造时读取）。"""
    import os

    if getattr(args, "mcp_config", None):
        os.environ["BEN_MCP_CONFIG"] = str(args.mcp_config)
    if getattr(args, "mcp_server", None):
        os.environ["BEN_MCP_ONLY"] = ",".join(args.mcp_server)
    if getattr(args, "mcp_exclude", None):
        os.environ["BEN_MCP_EXCLUDE"] = ",".join(args.mcp_exclude)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        from Ben.interface.cli.auth import run_auth

        return run_auth(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "view":
        from Ben.interface.viewer.cli import run_view

        return run_view(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("completion", "completions"):
        from Ben.interface.cli.completions import run_completions

        return run_completions(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("preflight", "check", "target-check"):
        from Ben.interface.cli.target_check import run_preflight

        return run_preflight(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("recon", "recon-only"):
        from Ben.interface.cli.recon import run_recon

        return run_recon(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("scout", "scout-only"):
        from Ben.interface.cli.scout import run_scout

        return run_scout(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "vuln":
        from Ben.interface.cli.vuln import run_vuln

        # 传入 _run_task，避免以 `__main__` 运行时二次导入本模块。
        return run_vuln(sys.argv[2:], run_task=_run_task)

    args = parse_arguments()

    if getattr(args, "config", None):
        from pathlib import Path

        from Ben.config.loader import apply_config_override

        apply_config_override(Path(args.config))

    _apply_runtime_env(args)
    _setup_logging(args.verbose)
    _apply_keypool_overrides()

    if getattr(args, "resume_picker", False):
        from rich.console import Console

        from Ben.core.paths import RUNS_DIR_NAME
        from Ben.interface.terminal.resume_picker import pick_run
        from Ben.report.runs import list_run_summaries

        runs = list_run_summaries()
        chosen = pick_run(runs, runs_dir=RUNS_DIR_NAME, console=Console())
        if chosen is None:
            return 0
        args.resume = chosen.run_name
        from Ben.interface.cli.arguments import load_resume_state

        load_resume_state(args)
        args.targets_info = getattr(args, "targets_info", [])

    if args.selftest_docker:
        return _selftest_docker()
    if args.selftest_llm:
        return _selftest_llm()
    if args.selftest_tools:
        return _selftest_tools()

    from Ben.interface.terminal.text import terminal_attached

    if not getattr(args, "non_interactive", False) and terminal_attached():
        import asyncio

        from Ben.interface.tui.runtime import GoTuiPreActivationError, run_go_tui
        from Ben.interface.tui.sidecar import TuiProcessExitedError

        try:
            asyncio.run(run_go_tui(args))
        except GoTuiPreActivationError as exc:
            print(f"[FAIL] TUI 启动失败：{exc}")
            print("[INFO] 已回退到 headless 模式。")
            return _run_task(args)
        except TuiProcessExitedError as exc:
            print(f"[FAIL] {exc}。可用 -n 以 headless 模式运行。")
            return 1
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # noqa: BLE001 - surface scan failures without a traceback
            print(f"[FAIL] 运行失败：{exc}")
            return 1
        return 0

    return _run_task(args)


if __name__ == "__main__":
    sys.exit(main())
