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

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Windows asyncio 兼容
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

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
from Ben.interface.cli_args import parse_args  # noqa: E402


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
            meta = next((l for l in lines if l.startswith("__BEN_META__")), "")
            status_code = meta.removeprefix("__BEN_META__") or "?"
            header_names = [
                l.split(":", 1)[0].strip()
                for l in lines
                if ":" in l and not l.startswith(("HTTP/", "__BEN_META__"))
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


def _run_task(args) -> int:
    """完整 Agent 任务（SDK Runner + SDK Sandbox 驱动）。

    流程（对齐 strix prepare_run 的 Ben 子集）：
      args.targets_info（parse 阶段已分类/校验；resume 时已从 run.json 恢复）
      → repository 目标 clone 到本地临时目录（resume 跳过，复用既有克隆）
      → collect_local_sources（沙箱 bind mount 清单）
      → resolve_diff_scope_context（--scope-mode/--diff-base）
      → diff instruction_block 前置拼入 instruction
      → 构造 AgentRunner 并运行
    """
    settings = load_settings(require_api_key=True)
    resume_run_name = getattr(args, "resume", None) or None
    interactive = bool(getattr(args, "interactive", False))
    targets_info = list(getattr(args, "targets_info", None) or [])
    instruction = getattr(args, "instruction", None)
    local_sources: list[dict] = []
    diff_scope: dict | None = None

    if resume_run_name is not None:
        # resume：targets_info/scan_mode/instruction/diff_scope/local_sources
        # 已在 parse 阶段由 load_resume_state 从该 run 的 run.json 恢复并校验
        # （含克隆仓库/本地目录仍在、agents.json 已到首个快照）。
        local_sources = list(getattr(args, "local_sources", None) or [])
        diff_scope = getattr(args, "diff_scope", None)
    elif not targets_info:
        print("未指定目标，使用白名单首项（runner 内回退）")
    else:
        from Ben.interface.targets import (
            clone_repository,
            collect_local_sources,
            resolve_diff_scope_context,
        )

        # repository 目标先 clone 到本地（对齐 strix prepare_run）。
        run_name = f"ben_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        for target_info in targets_info:
            if target_info.get("type") != "repository":
                continue
            details = target_info["details"]
            try:
                details["cloned_repo_path"] = clone_repository(
                    details["target_repo"], run_name, details.get("workspace_subdir")
                )
            except (ValueError, FileNotFoundError) as exc:
                print(f"[FAIL] 克隆仓库失败：{exc}")
                return 1

        local_sources = collect_local_sources(targets_info)

        # diff-scope 解析（Ben 始终非交互：auto 在 CI/PR 环境自动启用）。
        try:
            diff = resolve_diff_scope_context(
                local_sources=local_sources,
                scope_mode=getattr(args, "scope_mode", "auto"),
                diff_base=getattr(args, "diff_base", None),
                non_interactive=True,
            )
        except ValueError as exc:
            print(f"[FAIL] diff-scope 解析失败：{exc}")
            return 1
        diff_scope = diff.metadata
        # 对齐 strix prepare_run L266-271：diff 说明块前置拼入 instruction。
        if diff.instruction_block:
            if instruction:
                instruction = f"{diff.instruction_block}\n\n{instruction}"
            else:
                instruction = diff.instruction_block

    # SDK Runner 驱动的 Agent 运行器
    # 不再需要手传 DockerExecutor——runner 内部通过 SDK DockerSandboxClient 管理容器
    from Ben.core.runner import AgentRunner
    runner = AgentRunner(
        settings=settings,
        local_sources=local_sources or None,
        targets_info=targets_info or None,
        scan_mode=getattr(args, "scan_mode", "deep"),
        user_instructions=instruction,
        diff_scope=diff_scope,
        interactive=interactive,
        resume_run_name=resume_run_name,
        resume_instruction=getattr(args, "user_explicit_instruction", None),
    )

    try:
        result = runner.run(None)
    finally:
        # SDK sandbox session 在 runner 内部已关闭
        pass

    print("\n" + "=" * 70)
    print(result.report_text)
    print("=" * 70)
    print(f"结束原因：{result.stopped_reason}；工具调用：{len(result.invocations)} 次")
    print(f"报告文件：{result.report_path}")
    print(f"轨迹文件：{result.trace_path}")
    return 0


def main() -> int:
    args = parse_args()
    _setup_logging(args.verbose)

    if args.selftest_docker:
        return _selftest_docker()
    if args.selftest_llm:
        return _selftest_llm()
    if args.selftest_tools:
        return _selftest_tools()
    return _run_task(args)


if __name__ == "__main__":
    sys.exit(main())
