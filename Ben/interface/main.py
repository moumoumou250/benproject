"""BenProject 程序入口。

用法：
  # 正常任务（对授权目标跑完整 Agent 流程）
  .venv/Scripts/python.exe main.py http://117.72.199.2:3000/

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
import sys
from pathlib import Path

# Windows asyncio 兼容（对齐 strix 的 interface/main.py）
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 确保项目根在 sys.path 中（从任意 CWD 运行都能导入 Ben 包）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
    settings = load_settings()
    print(f"[1/2] 配置 SDK（model={settings.llm.model} @ {settings.llm.base_url}）...")

    from Ben.config import configure_sdk_model_defaults
    configure_sdk_model_defaults(settings)

    # 用 SDK 的 Runner 做最小连通测试（不使用 sandbox）
    from agents import RunConfig, Runner
    from agents.agent import Agent

    test_agent = Agent(
        name="connectivity-test",
        instructions="你是连通性测试助手，用一句话回答。",
        tools=[],
        model=settings.llm.model,
    )
    run_config = RunConfig(
        model=settings.llm.model,
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


def _run_task(target: str, *, keep_container: bool) -> int:
    """完整 Agent 任务（SDK Runner + SDK Sandbox 驱动）。"""
    settings = load_settings()
    if not target:
        target = settings.allowed_targets[0]
        print(f"未指定目标，使用白名单首项：{target}")

    # SDK Runner 驱动的 Agent 运行器
    # 不再需要手传 DockerExecutor——runner 内部通过 SDK DockerSandboxClient 管理容器
    from Ben.core.runner import AgentRunner
    runner = AgentRunner(settings=settings)

    try:
        result = runner.run(target)
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
    return _run_task(args.target, keep_container=args.keep_container)


if __name__ == "__main__":
    sys.exit(main())
