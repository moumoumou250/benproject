"""Repro: invoke SDK exec_command with full traceback (bypasses error-result wrapper)."""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.DEBUG)

import docker as docker_sdk  # noqa: E402
from agents.sandbox.manifest import Environment, Manifest  # noqa: E402
from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions  # noqa: E402

from Ben.config import load_settings  # noqa: E402
from Ben.runtime.docker_client import BenDockerSandboxClient  # noqa: E402


async def main() -> None:
    settings = load_settings()
    dc = docker_sdk.from_env()
    client = BenDockerSandboxClient(dc)
    opts = DockerSandboxClientOptions(image=settings.executor_image)
    manifest = Manifest(environment=Environment(value={"PYTHONUNBUFFERED": "1"}))
    session = await client.create(options=opts, manifest=manifest)
    try:
        from agents.sandbox.capabilities.shell import Shell

        from Ben.agents.factory import _configure_shell_tools

        cap = Shell(
            configure_tools=lambda toolset: _configure_shell_tools(
                toolset, allowed_targets=("http://117.72.199.2:3000",), strict_schemas=False
            )
        )
        cap.bind(session)
        tools = cap.tools()
        exec_tool = next(t for t in tools if getattr(t, "name", None) == "exec_command")
        print("TOOL:", exec_tool.name)
        try:
            result = await exec_tool.on_invoke_tool(None, '{"cmd": "echo hi"}')
            print("RESULT:", repr(result)[:500])
        except Exception:
            traceback.print_exc()
    finally:
        try:
            await session.aclose()
        except Exception:
            traceback.print_exc()
        dc.close()


if __name__ == "__main__":
    asyncio.run(main())
