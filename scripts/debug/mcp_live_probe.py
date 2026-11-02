"""用 Ben 自己的 MCP 运行时驱动四个派发工具，采集真实调用记录。

复刻 Ben/core/runner.py L744-772 的装配顺序：
    load_mcp_configs() -> McpConnectionRequest -> McpRegistry.register()
    -> start_warmup() -> 上下文注入 MCP_REGISTRY_CONTEXT_KEY

然后按 scope.jinja L37-42 的作业顺序真实调用：
    list_mcps -> search_mcp_tools -> get_mcp_tool_schema -> call_mcp

不依赖 Docker：MCP 连接走 stdio，与沙箱执行容器无关。
注意：stdio 传输需要 pipe，必须在无管道限制的沙箱模式下运行。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from agents import RunConfig  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402

from Ben.tools.mcp import (  # noqa: E402
    MCP_REGISTRY_CONTEXT_KEY,
    McpConnectionRequest,
    McpRegistry,
    call_mcp,
    get_mcp_tool_schema,
    list_mcps,
    load_mcp_configs,
    search_mcp_tools,
)

RECORDS: list[dict] = []
TAG = sys.argv[1] if len(sys.argv) > 1 else "default"


def _render(raw) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return repr(raw)


def _make_ctx(registry: McpRegistry, tool_name: str, args: str) -> ToolContext:
    """构造真实 ToolContext：invoker 会读 run_config 与 tool_arguments。"""
    return ToolContext(
        context={MCP_REGISTRY_CONTEXT_KEY: registry},
        tool_name=tool_name,
        tool_call_id=f"probe-{tool_name}",
        tool_arguments=args,
        run_config=RunConfig(),
    )


async def _call(label: str, tool, registry: McpRegistry, payload: dict) -> object:
    args = json.dumps(payload, ensure_ascii=False)
    print(f"\n{'=' * 72}\n[{label}] tool={tool.name}  arguments={args}\n{'=' * 72}",
          flush=True)
    try:
        result = await tool.on_invoke_tool(_make_ctx(registry, tool.name, args), args)
    except Exception as exc:
        text = f"<EXCEPTION {type(exc).__name__}: {exc}>"
        print(text, flush=True)
        RECORDS.append({"step": label, "tool": tool.name, "arguments": payload,
                        "ok": False, "result": text})
        return None
    print(_render(result), flush=True)
    RECORDS.append({"step": label, "tool": tool.name, "arguments": payload,
                    "ok": True, "result": result})
    return result


async def main() -> int:
    configs = load_mcp_configs()
    print(f"MCP config source: {Path.home() / '.ben' / 'mcp-servers.json'}")
    print(f"loaded {len(configs)} connection(s): {[c.name for c in configs]}", flush=True)
    if not configs:
        print("no connections; list_mcps would return an empty list.")
        return 0

    registry = McpRegistry()
    for config in configs:
        registry.register(McpConnectionRequest(config=config))

    warmup = registry.start_warmup(max_concurrency=6)
    try:
        await asyncio.wait({warmup}, timeout=120)
    except Exception as exc:
        print(f"warmup exception: {exc}")

    try:
        listed = await _call("1", list_mcps, registry, {})

        connection = None
        payload_obj = listed if isinstance(listed, dict) else {}
        for entry in payload_obj.get("connections", []) or []:
            if not entry.get("dead"):
                connection = entry.get("id") or entry.get("name")
                break
        if connection is None:
            print("\nno live connection; steps 2-4 cannot proceed.")
            return 1
        print(f"\n>>> selected connection: {connection!r}", flush=True)

        await _call("2", search_mcp_tools, registry,
                    {"connection": connection, "query": "list allowed directories"})

        await _call("3", get_mcp_tool_schema, registry,
                    {"connection": connection, "tool": "list_allowed_directories"})

        await _call("4", call_mcp, registry,
                    {"connection": connection, "tool": "list_allowed_directories",
                     "arguments": {}})
        return 0
    finally:
        try:
            await registry.close()
        except Exception as exc:
            print(f"registry close exception: {exc}")
        out = PROJECT_ROOT / "reports" / f"mcp_probe_records_{TAG}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(RECORDS, ensure_ascii=False, indent=2,
                                  default=str), encoding="utf-8")
        print(f"\nstructured records -> {out}", flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
