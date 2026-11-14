"""BenProject 程序入口。

用法：
  # 正常任务（对授权目标跑完整 Agent 流程）
  .venv/Scripts/python.exe main.py http://117.72.199.2:3000/

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
  .venv/Scripts/python.exe main.py http://117.72.199.2:3000/ --keep-container

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

from Ben.config import load_settings  # noqa: E402
from Ben.interface.cli.arguments import parse_arguments  # noqa: E402


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
    """模型文本连通验证：用 openai-agents SDK 发一条最小请求并打印回复。"""
    settings = load_settings(require_api_key=True)
    print(f"[1/2] 配置 SDK（model={settings.llm.model} @ {settings.llm.base_url}）...")

    from Ben.config import configure_sdk_model_defaults
    configure_sdk_model_defaults(settings)

    # 用 SDK 的 Runner 做最小连通测试（不使用 sandbox）
    # model_provider 挂 BenProvider，与正式运行同一路由（支持 厂商/模型 前缀）
    from agents import RunConfig, Runner
    from agents.agent import Agent
    from Ben.config.models import BenProvider

    test_agent = Agent(
        name="connectivity-test",
        instructions="你是连通性测试助手，用一句话回答。",
        tools=[],
        model=settings.llm.model,
    )
    run_config = RunConfig(
        model=settings.llm.model,
        model_provider=BenProvider(),
        trace_include_sensitive_data=False,
    )

    print("[2/2] 调用模型 ...")
    import asyncio

    async def _test() -> str:
        stream = Runner.run_streamed(
            test_agent,
            input="请回复：模型连接正常",
            run_config=run_config,
            max_turns=1,
        )
        async for _ in stream.stream_events():
            pass
        # 提取最终文本
        from agents.items import MessageOutputItem
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

    try:
        reply = asyncio.run(_test())
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1

    print(f"模型回复：{reply}")
    print("ALL OK - 模型文本调用正常")
    return 0


def _selftest_tools() -> int:
    """工具层验证：用 SDK sandbox 在容器内执行一次 curl 检查。"""
    settings = load_settings(require_api_key=False)
    target = settings.allowed_targets[0]

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
        print("未指定目标，使用白名单首项（runner 内回退）")

    # headless 实时视图（Ben.interface.cli.runner）内部构造 AgentRunner：
    # - 复用同一份 ReportState，漏洞发现即打印面板（对齐 strix 的 run_cli）；
    # - --verbose 时关闭 Live，避免日志与刷新互相覆盖；
    # - 非 TTY（CI/重定向）时自动降级为纯文本输出。
    import asyncio

    from Ben.interface.cli.runner import run_cli

    live = not bool(getattr(args, "verbose", False)) and sys.stdout.isatty()
    result = asyncio.run(run_cli(args, prepared, live=live))

    if result is None:
        print("[INFO] 本次运行未产出最终结果（预算耗尽或被中断），详见 strix_runs 目录。")
        return 0

    print("\n" + "=" * 70)
    print(result.report_text)
    print("=" * 70)
    print(f"结束原因：{result.stopped_reason}；工具调用：{len(result.invocations)} 次")
    print(f"报告文件：{result.report_path}")
    print(f"轨迹文件：{result.trace_path}")

    fail_on = getattr(args, "fail_on", None)
    if fail_on is not None:
        import json

        from Ben.core.paths import run_dir_for

        effective_run_name = resume_run_name or (run_name if "run_name" in locals() else "")
        if effective_run_name:
            vulnerabilities_path = run_dir_for(effective_run_name) / "vulnerabilities.json"
        else:
            vulnerabilities_path = result.report_path.parent / "vulnerabilities.json"
        try:
            findings = json.loads(vulnerabilities_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            findings = []
        if findings_fail_build(findings if isinstance(findings, list) else [], fail_on):
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

    args = parse_arguments()

    if getattr(args, "config", None):
        from pathlib import Path

        from Ben.config.loader import apply_config_override

        apply_config_override(Path(args.config))

    _apply_runtime_env(args)
    _setup_logging(args.verbose)

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
