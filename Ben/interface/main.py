"""BenProject 程序入口。

用法：
  # 正常任务（对授权目标跑完整 Agent 流程）
  .venv/Scripts/python.exe main.py http://juice-shop:3000/

  # 仅验证 Docker 执行器（不需要 API Key）
  .venv/Scripts/python.exe main.py --selftest-docker

  # 仅验证 DeepSeek 文本调用（需要 .env 中的 API Key）
  .venv/Scripts/python.exe main.py --selftest-llm

  # 仅验证工具层（不需要 API Key，会真实在容器内执行一次 HTTP 检查）
  .venv/Scripts/python.exe main.py --selftest-tools

  # 任务结束保留执行容器（默认自动删除）
  .venv/Scripts/python.exe main.py http://juice-shop:3000/ --keep-container
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 确保项目根在 sys.path 中（从任意 CWD 运行都能导入 Ben 包）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Ben.config import load_settings  # noqa: E402
from Ben.interface.cli_args import parse_args  # noqa: E402
from Ben.llm.deepseek_client import DeepSeekClient, LLMError  # noqa: E402
from Ben.runtime.docker_executor import DockerExecutor  # noqa: E402
from Ben.tools.registry import build_default_registry, ToolRuntime  # noqa: E402


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _selftest_docker() -> int:
    """Docker 执行器自检（复用其内置流程）。"""
    from Ben.runtime.docker_executor import _selftest

    return _selftest()


def _selftest_llm() -> int:
    """DeepSeek 文本连通验证：发一条最小请求并打印回复。"""
    settings = load_settings()
    client = DeepSeekClient(
        api_key=settings.deepseek_api_key,
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        timeout=settings.llm_timeout,
    )
    print(f"[1/2] 调用 {settings.deepseek_model} @ {settings.deepseek_base_url} ...")
    try:
        response = client.chat(
            [
                {"role": "system", "content": "你是连通性测试助手，用一句话回答。"},
                {"role": "user", "content": "请回复：DeepSeek 连接正常"},
            ]
        )
    except LLMError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[2/2] 模型回复：{response.text}")
    print("ALL OK - DeepSeek 文本调用正常")
    return 0


def _selftest_tools() -> int:
    """工具层验证：真实在容器内对授权目标执行一次 http_check。"""
    settings = load_settings(require_api_key=False)
    target = settings.allowed_targets[0]
    registry = build_default_registry()
    executor = DockerExecutor(
        image=settings.executor_image,
        network=settings.executor_network,
        container_name=settings.executor_container,
        auto_remove=True,
    )
    try:
        runtime = ToolRuntime(
            executor=executor,
            allowed_targets=settings.allowed_targets,
            command_timeout=settings.command_timeout,
        )
        print(f"对授权目标执行 http_check：{target}")
        result = registry.execute("http_check", {"url": target + "/"}, runtime)
        if not result.ok:
            print(f"[FAIL] {result.error}")
            return 1
        print(result.to_message())
        print("ALL OK - 工具层真实执行正常")
        return 0
    finally:
        executor.cleanup()


def _run_task(target: str, *, keep_container: bool) -> int:
    """完整 Agent 任务。"""
    settings = load_settings()
    if not target:
        target = settings.allowed_targets[0]
        print(f"未指定目标，使用白名单首项：{target}")

    # 组装各独立模块：模型客户端、工具注册表、Docker 执行器、Agent 运行器。
    llm = DeepSeekClient(
        api_key=settings.deepseek_api_key,
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        timeout=settings.llm_timeout,
    )
    # registry 仅一个_tools字段，注册了 
    # registry.__dict__ == {'_tools': {'http_check': HTTP_CHECK_SPEC}}
    registry = build_default_registry()

    executor = DockerExecutor(
        image=settings.executor_image,
        network=settings.executor_network,
        container_name=settings.executor_container,
        auto_remove=not keep_container,
    )

    from Ben.core.runner import AgentRunner
    runner = AgentRunner(
        settings=settings, llm=llm, registry=registry, executor=executor
    )
    try:
        result = runner.run(target)
    finally:
        executor.cleanup()

    print("\n" + "=" * 70)
    print(result.report_text)
    print("=" * 70)
    print(f"结束原因：{result.stopped_reason}；决策轮数：{result.turns_used}；"
          f"工具调用：{len(result.invocations)} 次")
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
