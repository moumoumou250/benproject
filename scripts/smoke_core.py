"""核心模块冒烟测试：只做导入与构造，不发任何模型请求（0 token 消耗）。

用法：.venv/Scripts/python.exe scripts/smoke_core.py
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MODULES = [
    # 配置
    "Ben.config",
    "Ben.config.settings",
    "Ben.config.loader",
    "Ben.config.models",
    # 提示词
    "Ben.agents.prompt",
    "Ben.agents.factory",
    # 核心编排
    "Ben.core.runner",
    # 运行时
    "Ben.runtime.docker_connection",
    "Ben.runtime.docker_executor",
    "Ben.runtime.docker_client",
    "Ben.runtime.session_manager",
    # LLM / 上下文管理
    "Ben.config.models",
    "Ben.llm.compaction",
    "Ben.llm.context_budget",
    "Ben.llm.request_log",
    "Ben.llm.warmup",
    # 工具层
    "Ben.tools.output_store",
    "Ben.tools.load_skill.tool",
    "Ben.tools.reporting.tool",
    "Ben.tools.coverage.tools",
    "Ben.tools.threat_model.tools",
    "Ben.tools.mcp.config",
    "Ben.tools.mcp.agent_tools",
    "Ben.tools.proxy.tools",
    "Ben.tools.todo.tools",
    "Ben.tools.notes.tools",
    "Ben.tools.web_search.tool",
    "Ben.tools.finish.tool",
    "Ben.tools.agents_graph.tools",
    # 报告
    "Ben.report",
    # 入口
    "Ben.interface.main",
    "Ben.interface.cli_args",
]


def main() -> int:
    ok, failed = [], []

    print("=" * 62)
    print("阶段 1：模块导入")
    print("=" * 62)
    for name in MODULES:
        try:
            importlib.import_module(name)
            ok.append(name)
            print(f"  [OK]   {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"  [FAIL] {name}: {exc.__class__.__name__}: {exc}")

    print()
    print("=" * 62)
    print("阶段 2：核心对象构造（不调用模型）")
    print("=" * 62)

    # 2.1 配置加载
    try:
        from Ben.config import load_settings

        settings = load_settings(require_api_key=False)
        print(f"  [OK]   load_settings: model={settings.llm.model}")
        print(f"         base_url={settings.llm.api_base}")
        print(f"         targets={settings.allowed_targets}")
        print(f"         max_turns={settings.llm.max_turns} timeout={settings.command_timeout}")
    except Exception as exc:  # noqa: BLE001
        failed.append(("load_settings", exc))
        print(f"  [FAIL] load_settings: {exc.__class__.__name__}: {exc}")

    # 2.2 提示词渲染
    try:
        from Ben.agents.prompt import render_system_prompt

        prompt = render_system_prompt(scan_mode="blackbox", is_root=True)
        print(f"  [OK]   render_system_prompt: {len(prompt)} 字符")
    except Exception as exc:  # noqa: BLE001
        failed.append(("render_system_prompt", exc))
        print(f"  [FAIL] render_system_prompt: {exc.__class__.__name__}: {exc}")

    # 2.3 工具定义（agent factory 中挂载的工具族）
    try:
        from Ben.agents.factory import _BASE_TOOLS, build_agent  # noqa: F401

        names = [t.name for t in _BASE_TOOLS]
        print(f"  [OK]   工具族: {len(names)} 个（{', '.join(names[:6])}...）")
    except Exception as exc:  # noqa: BLE001
        failed.append(("agent_tools", exc))
        print(f"  [FAIL] agent_tools: {exc.__class__.__name__}: {exc}")

    # 2.4 Docker 连接（只连不建容器）
    try:
        from Ben.runtime.docker_connection import connect_docker, docker_healthcheck

        client = connect_docker()
        health = docker_healthcheck(client)
        print(
            f"  [OK]   Docker 连接: ok={health.get('ok')} "
            f"server={health.get('server_version')} containers={health.get('containers_running')}"
        )
    except Exception as exc:  # noqa: BLE001
        failed.append(("docker_connection", exc))
        print(f"  [FAIL] docker_connection: {exc.__class__.__name__}: {exc}")

    # 2.5 技能加载
    try:
        from Ben.tools.load_skill.tool import load_skill  # noqa: F401
        from Ben.skills import list_skills  # type: ignore[attr-defined]

        print(f"  [OK]   skills: {len(list_skills())} 个知识片段")
    except Exception:
        skills_dir = PROJECT_ROOT / "Ben" / "skills"
        count = len(list(skills_dir.rglob("*.md"))) if skills_dir.exists() else 0
        print(f"  [OK]   skills 目录: {count} 个 .md 片段")

    print()
    print("=" * 62)
    if failed:
        print(f"结果：{len(ok)} 个模块导入成功，{len(failed)} 项失败")
        print("\n失败详情：")
        for name, exc in failed:
            print(f"\n--- {name} ---")
            traceback.print_exception(type(exc), exc, exc.__traceback__, limit=3)
        return 1
    print(f"结果：全部 {len(ok)} 个模块导入成功，核心对象构造正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
