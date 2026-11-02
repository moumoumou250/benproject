"""多 Agent 编排运行器（结构与流程对齐 strix 的 core/runner.py）。

用 ``AgentCoordinator`` + ``run_agent_loop``（Ben.core.execution，与 strix 同一实现）
驱动 root agent，root 可经 ``create_agent`` 图工具并行派生子 agent；
子 agent 完成时经 ``agent_finish`` 把完成报告投递到父 inbox。

流程（对齐 strix run_strix_scan）：
  校验目标（授权白名单，Ben 特有）
  -> 配置 SDK 全局默认值（configure_sdk_model_defaults）
  -> 创建 AgentCoordinator（agents.json 快照写入 reports_dir）
  -> 水合 todos/notes/coverage 磁盘状态（reports_dir 作 state_dir）
  -> 创建 SDK Docker sandbox（BenDockerSandboxClient + Caido sidecar）
  -> 配置 output_store spill writer（写沙箱 /workspace/.tool-output/）
  -> Caido bootstrap（后台并发，proxy 工具首次调用时才等待）
  -> 构建 root Agent（factory.build_agent，is_root=True，挂 finish_scan）
  -> make_child_factory + spawn_child_agent 闭包注入 ctx.context
  -> open_agent_session（SQLite agents.db，root/child 各一条会话）
  -> run_agent_loop（SDK Runner.run_streamed 多轮循环 + 生命周期回收）
  -> 收尾：cancel_descendants -> 关 session -> 关 Caido -> 关 sandbox
  -> 生成并保存 report.md / trace.json

与 strix 的差异（Ben 架构约束，刻意保留）：
- 普通 ``Agent`` + 手动绑定 Shell capability（chat_completions 路由，
  不用 SandboxAgent/hosted tools）；RunConfig 不带 SandboxRunConfig。
- 非交互自主运行（interactive=False），无 resume 流程；
  agents.json 快照仅供事后检查。
- Ben 特有：目标授权白名单校验、Caido 流量留痕、reports_dir 产出
  report/trace。

模块职责：
  - Ben.config              → .env 配置 + SDK 全局默认值
  - Ben.agents.factory      → 构建 root/child Agent（工具 + instructions）
  - Ben.core.agents         → AgentCoordinator（图状态/邮箱/快照）
  - Ben.core.execution      → run_agent_loop / spawn_child_agent
  - Ben.core.runner         → 编排：配置→sandbox→agent→coordinator→报告
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from agents import RunConfig
from agents.exceptions import MaxTurnsExceeded
from agents.items import (
    MessageOutputItem,
    RunItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.sandbox.manifest import Environment, Manifest
from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

from Ben.agents.factory import build_agent, make_child_factory
from Ben.agents.prompt import build_system_prompt_context
from Ben.config import Settings, configure_sdk_model_defaults
from Ben.config.models import (
    BenProvider,
    model_supports_images,
    supports_strict_tool_schemas,
)
from Ben.core.agents import AgentCoordinator
from Ben.core.execution import run_agent_loop
from Ben.core.execution import spawn_child_agent as start_child_agent
from Ben.core.hooks import (
    SUBAGENT_TURN_LIMITS_KEY,
    BudgetExceededError,
    ReportUsageHooks,
)
from Ben.core.inputs import build_root_task, build_scan_targets, make_model_settings
from Ben.core.sessions import open_agent_session
from Ben.runtime.caido import (
    CONTAINER_CAIDO_PORT,
    CaidoBootstrapHandle,
    bootstrap_caido,
)
from Ben.runtime.docker_client import BenDockerSandboxClient

logger = logging.getLogger(__name__)


@dataclass
class ToolInvocation:
    """一次工具调用的完整记录（用于轨迹与报告）。"""
    name: str
    arguments: str
    output: str


@dataclass
class AgentRunResult:
    """一次 Agent 任务的最终交付。"""
    target: str
    report_text: str
    report_path: Path
    trace_path: Path
    turns_used: int
    stopped_reason: str
    invocations: list[ToolInvocation] = field(default_factory=list)


def _read_agent_session_items(agents_db: Path, agent_id: str) -> list[dict[str, Any]]:
    """读回某个 agent 的会话条目（agents.db，只读）。

    root 与子 agent 的完整工具调用只存在于各自 SQLite 会话里：SDK 的
    ``result.new_items`` 会随收尾轮变短，日志也只打 root。
    """
    import sqlite3

    if not agents_db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{agents_db}?mode=ro", uri=True)
    except sqlite3.Error:
        logger.exception("读取 agents.db 失败：%s", agents_db)
        return []
    try:
        rows = conn.execute(
            "select message_data from agent_messages where session_id = ? order by id",
            (agent_id,),
        )
        items: list[dict[str, Any]] = []
        for (data,) in rows:
            try:
                payload = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                items.append(payload)
        return items
    except sqlite3.Error:
        logger.exception("读取 agent %s 的会话失败", agent_id)
        return []
    finally:
        conn.close()


def _invocations_from_session_items(items: list[dict[str, Any]]) -> list[ToolInvocation]:
    """把会话条目里的 function_call / function_call_output 配成工具调用记录。"""
    outputs: dict[str, str] = {}
    for item in items:
        if item.get("type") == "function_call_output":
            call_id = item.get("call_id")
            if isinstance(call_id, str):
                outputs[call_id] = str(item.get("output") or "")
    invocations: list[ToolInvocation] = []
    for item in items:
        if item.get("type") != "function_call":
            continue
        call_id = item.get("call_id")
        invocations.append(
            ToolInvocation(
                name=str(item.get("name") or "?"),
                arguments=str(item.get("arguments") or ""),
                output=outputs.get(call_id, "") if isinstance(call_id, str) else "",
            )
        )
    return invocations


def _final_text_from_session_items(items: list[dict[str, Any]]) -> str:
    """取最后一条 assistant 文本消息（root 的最终报告）。"""
    for item in reversed(items):
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("text")
            ]
            if texts:
                return "\n".join(texts)
    return ""


def _iter_tool_call_names(obj: Any):
    """递归找出 SDK 会话条目里的 function call 名称。"""
    stack: list[Any] = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("type") in {"function_call", "tool_call"}:
                fn = item.get("function")
                name = item.get("name") or (fn.get("name") if isinstance(fn, dict) else None)
                if isinstance(name, str) and name:
                    yield name
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _tool_call_counts_by_agent(agents_db: Path, agent_ids: list[str]) -> dict[str, dict[str, int]]:
    """按 agent_id 统计其会话内的工具调用次数（读 agents.db，只读）。

    子 agent 的工具调用只存在于各自的 SQLite 会话里：root 的 SDK items 看不到，
    日志也只打 root。这里读回来写进 trace，便于事后确认 think/notes/todo、
    create_agent 等工具是否真的被调用过。
    """
    counts: dict[str, dict[str, int]] = {}
    for agent_id in agent_ids:
        agent_counts: dict[str, int] = {}
        for payload in _read_agent_session_items(agents_db, agent_id):
            for name in _iter_tool_call_names(payload):
                agent_counts[name] = agent_counts.get(name, 0) + 1
        counts[agent_id] = agent_counts
    return counts


def _short_args(args_str: str, max_len: int = 80) -> str:
    """从工具调用参数 JSON 中提取 URL 或关键参数，用于日志显示。"""
    if not args_str:
        return ""
    try:
        args = json.loads(args_str) if isinstance(args_str, str) else args_str
        if isinstance(args, dict):
            url = args.get("url", "")
            if url:
                return url
            return json.dumps(args, ensure_ascii=False)[:max_len]
    except (json.JSONDecodeError, TypeError):
        pass
    return args_str[:max_len] if isinstance(args_str, str) else str(args_str)[:max_len]


def _check_target_allowed(target: str, allowed: tuple[str, ...]) -> None:
    """校验目标在授权白名单内。"""
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"目标必须是 http/https URL：{target!r}")
    origin = (
        f"{parts.scheme}://{parts.hostname}:{parts.port}"
        if parts.port is not None
        else f"{parts.scheme}://{parts.hostname}"
    )
    allowed_origins = set()
    for item in allowed:
        p = urlsplit(item)
        if p.hostname:
            allowed_origins.add(
                f"{p.scheme}://{p.hostname}:{p.port}"
                if p.port is not None
                else f"{p.scheme}://{p.hostname}"
            )
    if origin not in allowed_origins:
        raise ValueError(
            f"目标 {origin} 不在授权白名单 {sorted(allowed_origins)} 内，拒绝启动"
        )


def _extract_tool_invocations(items: list[RunItem]) -> list[ToolInvocation]:
    """从 SDK 的 run items 中提取工具调用记录。

    SDK 在 chat_completions 模式下，ToolCallItem.raw_item 是
    ResponseFunctionToolCall（有 .name/.arguments 属性），
    ToolCallOutputItem.raw_item 是 dict（output 存在 "output" 键下）。
    """
    invocations: list[ToolInvocation] = []
    pending_calls: dict[str, ToolInvocation] = {}  # call_id -> ToolInvocation

    for item in items:
        raw = getattr(item, "raw_item", None)

        # ToolCallItem: 模型发起的工具调用
        if isinstance(item, ToolCallItem):
            call_name = ""
            call_args = ""
            call_id = ""
            if raw is not None:
                # responses API 格式（ResponseFunctionToolCall）
                name_attr = getattr(raw, "name", None)
                args_attr = getattr(raw, "arguments", None)
                if name_attr:
                    call_name = name_attr
                if args_attr:
                    call_args = args_attr if isinstance(args_attr, str) else json.dumps(args_attr)
                call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None) or ""
                # chat_completions API 格式（ChatCompletionMessageToolCall）
                if not call_name:
                    func = getattr(raw, "function", None)
                    if func:
                        call_name = getattr(func, "name", "")
                        call_args = getattr(func, "arguments", "") or ""
                        call_id = getattr(raw, "id", "") or ""
            inv = ToolInvocation(name=call_name, arguments=call_args, output="")
            if call_id:
                pending_calls[call_id] = inv
            invocations.append(inv)

        # ToolCallOutputItem: 工具执行结果
        elif isinstance(item, ToolCallOutputItem):
            output_text = ""
            call_id = ""
            if raw is not None:
                # dict 格式（chat_completions 模式）
                if isinstance(raw, dict):
                    output_text = str(raw.get("output", "") or "")
                    call_id = raw.get("call_id", "") or ""
                else:
                    # 对象格式（responses API）
                    output_attr = getattr(raw, "output", None)
                    if output_attr:
                        output_text = output_attr if isinstance(output_attr, str) else str(output_attr)
                    call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None) or ""

            # 尝试通过 call_id 匹配
            if call_id and call_id in pending_calls:
                pending_calls[call_id].output = output_text
            else:
                # 回退：匹配最近的未填充输出的 ToolInvocation
                for inv in reversed(invocations):
                    if inv.name != "unknown" and not inv.output:
                        inv.output = output_text
                        break
                else:
                    invocations.append(ToolInvocation(name="unknown", arguments="", output=output_text))

    return invocations


def _extract_final_text(items: list[RunItem]) -> str:
    """从 run items 中提取最终的文本输出。"""
    texts: list[str] = []
    for item in items:
        if isinstance(item, MessageOutputItem):
            raw = getattr(item, "raw_item", None)
            if raw is not None:
                content = getattr(raw, "content", None)
                if content:
                    if isinstance(content, str):
                        texts.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            text_val = getattr(part, "text", None) if hasattr(part, "text") else None
                            if text_val:
                                texts.append(text_val)
                            elif isinstance(part, str):
                                texts.append(part)
    return "\n".join(texts) if texts else ""


class AgentRunner:
    """多 Agent 编排运行器（对齐 strix run_strix_scan）。

    模块协作流程：
      1. Ben.config.settings → 读取 .env，校验配置
      2. configure_sdk_model_defaults → 注入 API Key/Base URL/chat_completions
      3. AgentCoordinator → 图状态/邮箱/agents.json 快照
      4. BenDockerSandboxClient → 创建容器 sandbox session（Caido sidecar）
      5. Ben.agents.factory → 构建 root Agent（shell 工具绑定 session）
         + make_child_factory（create_agent 图工具的子 agent 来源）
      6. Ben.core.execution.run_agent_loop → SDK Runner 多轮循环
      7. 收尾：子 agent 取消 → session 关闭 → Caido 关闭 → 容器清理
      8. 保存 report.md + trace.json
    """

    def __init__(
        self,
        *,
        settings: Settings,
    ) -> None:
        self.settings = settings

    def run(self, target: str) -> AgentRunResult:
        """同步入口：执行完整 Agent 任务。"""
        return asyncio.run(self.run_async(target))

    # ------------------------------------------------------------------
    # 流式事件日志（event_sink：root 与 child 共用）
    # ------------------------------------------------------------------

    def _make_event_sink(self, max_turns: int) -> "tuple[callable, dict[str, int]]":
        """按 agent 统计轮次的流式日志 sink（对齐 strix 的 event_sink 注入点）。"""
        turn_counts: dict[str, int] = {}
        prev_item_type: dict[str, str | None] = {}

        def sink(agent_id: str, event: object) -> None:
            item = getattr(event, "item", None)
            if item is None:
                return
            item_type = type(item).__name__
            if isinstance(item, ToolCallItem):
                if prev_item_type.get(agent_id) != "ToolCallItem":
                    turn_counts[agent_id] = turn_counts.get(agent_id, 0) + 1
                tool_name = ""
                raw = getattr(item, "raw_item", None)
                if raw:
                    name_attr = getattr(raw, "name", None)
                    if name_attr:
                        tool_name = name_attr
                    else:
                        func = getattr(raw, "function", None)
                        if func:
                            tool_name = getattr(func, "name", "")
                logger.info(
                    "[agent %s][Turn %d/%d] 工具调用: %s → %s",
                    agent_id, turn_counts[agent_id], max_turns,
                    tool_name or "?",
                    _short_args(getattr(raw, "arguments", "") or ""),
                )
            elif isinstance(item, ToolCallOutputItem):
                logger.info(
                    "[agent %s][Turn %d/%d] 工具结果返回",
                    agent_id, turn_counts.get(agent_id, 0), max_turns,
                )
            elif isinstance(item, MessageOutputItem):
                logger.info(
                    "[agent %s][Turn %d/%d] 模型输出文本消息",
                    agent_id, turn_counts.get(agent_id, 0), max_turns,
                )
            prev_item_type[agent_id] = item_type

        return sink, turn_counts

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run_async(self, target: str) -> AgentRunResult:
        """异步执行多 Agent 编排任务（核心编排函数，串联所有模块）。"""
        started = time.monotonic()
        settings = self.settings

        # === 阶段 1: 校验目标（Ben 特有：授权白名单） ===
        logger.info("=" * 60)
        logger.info("阶段 1/6: 校验目标")
        logger.info("=" * 60)
        if not target:
            target = settings.allowed_targets[0]
            logger.info("未指定目标，使用白名单首项：%s", target)
        _check_target_allowed(target, settings.allowed_targets)
        logger.info("目标校验通过：%s", target)

        # === 阶段 2: 配置 SDK 全局默认值 ===
        logger.info("=" * 60)
        logger.info("阶段 2/6: 配置 openai-agents SDK（Ben.config.settings）")
        logger.info("=" * 60)
        configure_sdk_model_defaults(settings)
        resolved_model = settings.llm.model
        strict_tool_schemas = supports_strict_tool_schemas(resolved_model)
        supports_images = model_supports_images(resolved_model)
        if not supports_images:
            logger.info("模型 %s 不支持图片输入，跳过图片工具", resolved_model)
        logger.info("SDK 配置完成：model=%s, api_route=chat_completions", resolved_model)

        # === 阶段 3: 创建 AgentCoordinator + 水合磁盘状态 ===
        # 对齐 strix core/runner.py：reports_dir 作 state_dir，
        # agents.json 快照 + SQLite agents.db 均落在此处。
        logger.info("=" * 60)
        logger.info("阶段 3/6: 创建 AgentCoordinator（多 agent 图状态）")
        logger.info("=" * 60)
        state_dir = settings.reports_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        agents_path = state_dir / "agents.json"
        agents_db = state_dir / "agents.db"

        coordinator = AgentCoordinator()
        coordinator.set_snapshot_path(agents_path)
        coordinator.root_finish_tool = "finish_scan"

        from Ben.tools.coverage.tools import hydrate_coverage_from_disk
        from Ben.tools.notes.tools import hydrate_notes_from_disk
        from Ben.tools.todo.tools import hydrate_todos_from_disk

        hydrate_todos_from_disk(state_dir)
        hydrate_notes_from_disk(state_dir)
        hydrate_coverage_from_disk(state_dir)

        # === 阶段 4: 创建 SDK Docker Sandbox（含 Caido 代理 sidecar） ===
        logger.info("=" * 60)
        logger.info("阶段 4/6: 创建 Docker Sandbox（BenDockerSandboxClient + Caido sidecar）")
        logger.info("=" * 60)
        import docker as docker_sdk

        dc = docker_sdk.from_env()
        # BenDockerSandboxClient 保留镜像 ENTRYPOINT（启动 caido-cli、写入
        # 系统级代理环境变量），追加 NET_ADMIN/NET_RAW 与日志上限
        sandbox_client = BenDockerSandboxClient(dc)
        # Caido 作为容器内 sidecar 运行；session.exec 启动的进程自动继承
        # 这些 env，HTTP(S) 流量全部经 Caido 留痕。NO_PROXY 防止
        # agent-browser 的 localhost CDP 流量回环进 Caido。
        container_caido_url = f"http://127.0.0.1:{CONTAINER_CAIDO_PORT}"
        sandbox_manifest = Manifest(
            environment=Environment(
                value={
                    "PYTHONUNBUFFERED": "1",
                    "http_proxy": container_caido_url,
                    "https_proxy": container_caido_url,
                    "ALL_PROXY": container_caido_url,
                    "NO_PROXY": "localhost,127.0.0.1",
                },
            ),
        )
        sandbox_opts = DockerSandboxClientOptions(
            image=settings.executor_image,
            exposed_ports=(CONTAINER_CAIDO_PORT,),
        )
        logger.info(
            "创建 sandbox session（image=%s, caido_port=%d）...",
            settings.executor_image,
            CONTAINER_CAIDO_PORT,
        )
        sandbox_session = await sandbox_client.create(
            options=sandbox_opts, manifest=sandbox_manifest
        )

        # === 阶段 5: 构建 root Agent + 子 agent 工厂 ===
        logger.info("=" * 60)
        logger.info("阶段 5/6: 构建 Agent（Ben.agents.factory，root 挂 finish_scan）")
        logger.info("=" * 60)

        max_turns = settings.max_agent_turns
        stopped_reason = "未知"
        final_text = ""
        invocations: list[ToolInvocation] = []
        turns_used = max_turns
        caido_handle: CaidoBootstrapHandle | None = None
        sessions_to_close: list = []
        root_id = uuid.uuid4().hex[:8]

        # strix 形态的 scan_config（Ben 目前只有单一 web 目标）
        scan_config: dict = {
            "targets": [{"type": "web_application", "details": {"target_url": target}}],
            "scan_mode": "blackbox",
            "user_instructions": (
                f"授权范围：{', '.join(settings.allowed_targets)}。"
                "请按预设流程，先理解目标，再选择合适的工具取证，"
                "最后输出有真实证据支持的测试报告。"
            ),
        }
        root_task = build_root_task(scan_config)
        prompt_context = build_system_prompt_context(settings, target)

        # 报告状态（对齐 strix run_scan）：finish_scan / agent_finish /
        # coverage / SARIF 都经模块级 ReportState 取扫描产物；缺了它这些写入
        # 全部空转（finish_scan 会回 "not persisted"，子 agent 的发现也归档不了）。
        from Ben.report import ReportState, set_global_report_state

        run_name = f"ben_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{root_id}"
        report_state = ReportState(run_name=run_name)
        report_state.set_scan_config(scan_config)
        set_global_report_state(report_state)
        logger.info(
            "ReportState 已初始化：run=%s，产物目录=%s",
            report_state.run_id,
            report_state.get_run_dir(),
        )

        try:
            async with sandbox_session:
                logger.info("Sandbox session 已启动，容器就绪")

                # 超大工具结果 spill 到沙箱工作区（对齐 strix
                # _spill_to_workspace）：bound_and_store 经此 writer 写
                # /workspace/.tool-output/<id>.txt，agent 用 exec_command 读回。
                from Ben.tools.output_store import (
                    WORKSPACE_SPILL_DIR,
                    configure_spill_writer,
                )

                async def _spill_to_workspace(output_id: str, text: str) -> str | None:
                    path = f"{WORKSPACE_SPILL_DIR}/{output_id}.txt"
                    try:
                        await sandbox_session.write(Path(path), io.BytesIO(text.encode("utf-8")))
                    except Exception:
                        logger.exception("failed to spill tool output to sandbox workspace")
                        return None
                    return path

                configure_spill_writer(_spill_to_workspace)

                # Caido bootstrap 与 runner 并发（strix 的 CaidoBootstrapHandle
                # 模式）：proxy 工具首次调用时才经 handle.get() 等待结果。
                caido_endpoint = await sandbox_session.resolve_exposed_port(
                    CONTAINER_CAIDO_PORT
                )
                scheme = "https" if caido_endpoint.tls else "http"
                host_caido_url = f"{scheme}://{caido_endpoint.host}:{caido_endpoint.port}"
                caido_handle = CaidoBootstrapHandle(
                    asyncio.create_task(
                        bootstrap_caido(
                            sandbox_session,
                            host_url=host_caido_url,
                            container_url=container_caido_url,
                        ),
                        name="caido-bootstrap",
                    )
                )
                logger.info("Caido bootstrap 已启动（host=%s）", host_caido_url)

                model_settings = make_model_settings(
                    settings.llm.reasoning_effort,
                    model_name=resolved_model,
                    parallel_tool_calls=settings.llm.parallel_tool_calls,
                    request_timeout=settings.llm.timeout,
                    prompt_cache=settings.llm.prompt_cache,
                    extra_headers=settings.llm.extra_headers,
                    extra_body=settings.llm.extra_body,
                )
                run_config = RunConfig(
                    model=resolved_model,
                    model_settings=model_settings,
                    # 对齐 strix：带前缀模型（deepseek/...）经 LiteLLM 路由
                    model_provider=BenProvider(),
                    trace_include_sensitive_data=False,
                    # 幻觉工具名是可恢复的模型错误：作为工具结果回传，让模型自我纠正
                    tool_not_found_behavior="return_error_to_model",
                )
                hooks = ReportUsageHooks(
                    model=resolved_model,
                    max_turns=max_turns,
                    interactive=False,
                    record_tool_timings=settings.telemetry.run_record_timings,
                )

                root_agent = build_agent(
                    name="Root Agent",
                    skills=[],
                    is_root=True,
                    sandbox_session=sandbox_session,
                    allowed_targets=settings.allowed_targets,
                    scan_mode="blackbox",
                    strict_tool_schemas=strict_tool_schemas,
                    system_prompt_context=prompt_context,
                    supports_images=supports_images,
                    coverage_tools=True,
                    prompts_dir=settings.prompts_dir,
                )
                logger.info(
                    "Root Agent 已构建：%s, tools=%d",
                    root_agent.name,
                    len(root_agent.tools),
                )

                await coordinator.register(
                    root_id,
                    "Root Agent",
                    parent_id=None,
                    task=root_task,
                    skills=[],
                )

                # 子 agent 工厂（create_agent 图工具经 ctx.context 的
                # spawn_child_agent 闭包调用；对齐 strix runner）
                child_agent_builder = make_child_factory(
                    sandbox_session=sandbox_session,
                    allowed_targets=settings.allowed_targets,
                    scan_mode="blackbox",
                    strict_tool_schemas=strict_tool_schemas,
                    system_prompt_context=prompt_context,
                    supports_images=supports_images,
                    coverage_tools=True,
                    prompts_dir=settings.prompts_dir,
                )

                async def spawn_child_agent(**kwargs):  # noqa: ANN202, ANN003
                    return await start_child_agent(
                        coordinator=coordinator,
                        factory=child_agent_builder,
                        agents_db_path=agents_db,
                        sessions_to_close=sessions_to_close,
                        run_config=run_config,
                        max_turns=max_turns,
                        interactive=False,
                        hooks=hooks,
                        **kwargs,
                    )

                # 运行时上下文（图工具/notes/todo/coverage 经 ctx.context 取
                # coordinator 与身份；proxy 工具取 caido_client）
                context: dict = {
                    "coordinator": coordinator,
                    "sandbox_session": sandbox_session,
                    "caido_client": caido_handle,
                    "agent_id": root_id,
                    "parent_id": None,
                    "interactive": False,
                    "spawn_child_agent": spawn_child_agent,
                    "scan_targets": build_scan_targets(scan_config),
                    "max_context_images": settings.runtime.max_context_images,
                    "supports_images": supports_images,
                    "coverage_tools": True,
                    "allowed_targets": settings.allowed_targets,
                    "command_timeout": settings.command_timeout,
                    "target": target,
                    SUBAGENT_TURN_LIMITS_KEY: {},
                }

                root_session = open_agent_session(root_id, agents_db)
                sessions_to_close.append(root_session)
                await coordinator.attach_runtime(root_id, session=root_session)

                # === 阶段 6: 启动多 agent 循环 ===
                logger.info("=" * 60)
                logger.info(
                    "阶段 6/6: 启动 run_agent_loop（model=%s, max_turns=%d, root=%s）",
                    resolved_model, max_turns, root_id,
                )
                logger.info("=" * 60)

                event_sink, turn_counts = self._make_event_sink(max_turns)

                try:
                    result = await run_agent_loop(
                        agent=root_agent,
                        initial_input=root_task,
                        run_config=run_config,
                        context=context,
                        max_turns=max_turns,
                        coordinator=coordinator,
                        agent_id=root_id,
                        interactive=False,
                        session=root_session,
                        event_sink=event_sink,
                        hooks=hooks,
                    )
                except MaxTurnsExceeded:
                    logger.info("[Runner] 达到最大轮数（%d），正常停止", max_turns)
                    stopped_reason = f"达到最大轮数（{max_turns} 轮）"
                    result = None

                # 停止原因：优先看 coordinator 中 root 的终态（对齐 strix：
                # 生命周期工具把 root 标记 completed）
                if stopped_reason == "未知":
                    async with coordinator._lock:
                        root_status = coordinator.statuses.get(root_id)
                    if root_status == "completed":
                        stopped_reason = "模型调用 finish_scan 完成扫描"
                    elif root_status == "stopped":
                        stopped_reason = "达到最大轮数或被停止"
                    elif root_status in ("crashed", "failed"):
                        async with coordinator._lock:
                            err = coordinator.errors.get(root_id, "")
                        stopped_reason = f"Agent {root_status}：{err[:200]}"
                    else:
                        stopped_reason = f"运行结束（root 状态：{root_status}）"

                # 从 root 的 run 结果提取 items（即使异常也已落 SQLite session）
                items: list[RunItem] = []
                if result is not None and hasattr(result, "new_items"):
                    items = list(result.new_items)

                invocations = _extract_tool_invocations(items)
                final_text = _extract_final_text(items)

                # SDK 的 ``result.new_items`` 只保留最后一轮的条目（收尾轮往往
                # 只有 2-3 条），报告的"工具调用次数"和轨迹会因此严重偏少。
                # root 会话里的 function_call / function_call_output 才是完整
                # 事实来源，用它在 items 不完整时补齐。
                session_items = _read_agent_session_items(agents_db, root_id)
                if session_items:
                    session_invocations = _invocations_from_session_items(session_items)
                    if len(session_invocations) > len(invocations):
                        invocations = session_invocations
                    session_final = _final_text_from_session_items(session_items)
                    if session_final:
                        final_text = session_final

                turns_used = max(turn_counts.get(root_id, 0), 1)
                logger.info(
                    "[Runner] root 共 %d 轮，%d 次工具调用（items=%d，session=%d 条）",
                    turns_used,
                    len(invocations),
                    len(items),
                    len(session_items),
                )

                if result is not None:
                    final = getattr(result, "final_output", None)
                    async with coordinator._lock:
                        root_completed = coordinator.statuses.get(root_id) == "completed"
                    if not root_completed:
                        logger.error(
                            "运行结束但 root 未调用 finish_scan；"
                            "agent 以纯文本收尾而非生命周期工具调用，"
                            "未写入执行报告。最终输出（前 300 字符）：%r",
                            str(final)[:300],
                        )

        except BudgetExceededError as exc:
            logger.info("扫描达到预算上限，停止：%s", exc)
            stopped_reason = f"预算上限：{exc}"
            with contextlib.suppress(Exception):
                await coordinator.set_status(root_id, "stopped")
        except Exception as exc:
            logger.error("Agent 运行失败：%s", exc, exc_info=True)
            stopped_reason = f"运行错误：{exc}"
            with contextlib.suppress(Exception):
                await coordinator.set_status(root_id, "failed")

        finally:
            # 对齐 strix 收尾顺序：先取消后代（干净结束时 child 可能仍在
            # 回合中，直接关 session 会让它崩溃），再关 session/Caido/sandbox
            coordinator.mark_shutting_down()
            with contextlib.suppress(Exception):
                await coordinator.cancel_descendants(root_id)
            for s in sessions_to_close:
                with contextlib.suppress(Exception):
                    s.close()
            with contextlib.suppress(Exception):
                await coordinator._maybe_snapshot()
            if caido_handle is not None:
                try:
                    await caido_handle.aclose()
                    logger.info("Caido client 已关闭")
                except Exception:
                    pass
            try:
                await sandbox_session.aclose()
                logger.info("Sandbox session 已关闭，容器已清理")
            except Exception:
                pass
            from Ben.tools.output_store import configure_spill_writer

            configure_spill_writer(None)

        elapsed = time.monotonic() - started
        logger.info("=" * 60)
        logger.info(
            "循环结束（%s），共 %d 次工具调用，耗时 %.1fs",
            stopped_reason, len(invocations), elapsed,
        )
        logger.info("=" * 60)

        # 多 agent 工具使用汇总（含子 agent，来自 agents.db 的会话记录），
        # 写进 trace.json——root 的 invocations 只覆盖 root 自己那一层。
        agents_summary = await self._summarize_agents(coordinator, agents_db)

        # 报告状态收尾：finish_scan 成功时 update_scan_final_fields 已经
        # save_run_data(mark_complete=True)；未完成则落一条 stopped/failed 记录。
        try:
            if report_state.final_scan_result is None:
                status = "failed" if stopped_reason.startswith("运行错误") else "stopped"
                report_state.cleanup(status=status)
                logger.info("报告状态已收尾：status=%s，产物目录=%s", status, report_state.get_run_dir())
            else:
                logger.info(
                    "报告状态已完成：产物目录=%s（%d 份漏洞报告）",
                    report_state.get_run_dir(),
                    len(report_state.vulnerability_reports),
                )
        except Exception:
            logger.exception("报告状态收尾失败（不影响报告文件）")
        finally:
            set_global_report_state(None)

        if not final_text and report_state.final_scan_result:
            # finish_scan 落盘的四段式报告是客户可见版本，优先于兜底模板。
            final_text = report_state.final_scan_result
        if not final_text:
            final_text = self._generate_report(target, invocations, stopped_reason)

        report_path, trace_path = self._persist(
            target,
            final_text,
            invocations,
            stopped_reason,
            elapsed,
            agents_summary=agents_summary,
        )
        return AgentRunResult(
            target=target,
            report_text=final_text,
            report_path=report_path,
            trace_path=trace_path,
            turns_used=turns_used,
            stopped_reason=stopped_reason,
            invocations=invocations,
        )

    @staticmethod
    async def _summarize_agents(coordinator: Any, agents_db: Path) -> list[dict[str, Any]]:
        """每个 agent 的（子 agent 也含）工具调用统计，供 trace.json 复核。

        root 的 ``invocations`` 由 SDK items 抽取，只覆盖 root 自己；子 agent 的
        调用落在各自 SQLite 会话里，这里按 session_id 汇总成 name -> count。
        """
        try:
            snap = await coordinator.snapshot()
        except Exception:
            logger.exception("agent 图快照失败，跳过子 agent 统计")
            return []

        statuses = snap.get("statuses", {})
        parents = snap.get("parent_of", {})
        names = snap.get("names", {})
        counts = _tool_call_counts_by_agent(agents_db, list(statuses))
        return [
            {
                "agent_id": aid,
                "name": names.get(aid, aid),
                "parent_id": parents.get(aid),
                "status": statuses.get(aid),
                "tool_calls": sum(counts.get(aid, {}).values()),
                "tool_call_counts": counts.get(aid, {}),
            }
            for aid in statuses
        ]

    @staticmethod
    def _generate_report(
        target: str, invocations: list[ToolInvocation], reason: str
    ) -> str:
        """基于真实执行记录生成测试报告（模型未输出最终报告时使用）。"""
        lines = [
            "# 安全测试报告",
            "",
            f"**测试目标**：{target}",
            f"**终止原因**：{reason}",
            f"**工具调用次数**：{len(invocations)}",
            "",
            "## 测试操作记录",
            "",
        ]

        if not invocations:
            lines.append("- 无成功执行的工具调用")
        else:
            for i, inv in enumerate(invocations, 1):
                # 尝试解析参数中的 URL
                url_info = ""
                try:
                    args = json.loads(inv.arguments) if inv.arguments else {}
                    if isinstance(args, dict) and "url" in args:
                        method = args.get("method", "GET")
                        url_info = f" [{method}] {args['url']}"
                except (json.JSONDecodeError, TypeError):
                    pass

                # 尝试解析输出中的关键信息
                output_summary = ""
                if inv.output:
                    try:
                        out = json.loads(inv.output)
                        if isinstance(out, dict):
                            status = out.get("status_code", "")
                            missing = out.get("missing_security_headers", [])
                            if status:
                                output_summary = f" → HTTP {status}"
                            if missing:
                                output_summary += f"（缺失安全头: {', '.join(missing[:3])}）"
                    except (json.JSONDecodeError, TypeError):
                        output_summary = f" → {inv.output[:100]}"

                lines.append(f"{i}. **{inv.name}**{url_info}{output_summary}")

        lines += [
            "",
            "## 测试结论",
            "",
        ]

        # 分析工具输出中的安全发现
        security_findings = []
        for inv in invocations:
            if inv.output:
                try:
                    out = json.loads(inv.output)
                    if isinstance(out, dict):
                        missing = out.get("missing_security_headers", [])
                        if missing:
                            security_findings.append(
                                f"- {inv.arguments}: 缺失安全响应头 {missing}"
                            )
                except (json.JSONDecodeError, TypeError):
                    pass

        if security_findings:
            lines.append("### 安全加固建议")
            lines.extend(security_findings)
        else:
            lines.append("- 本次测试未发现可确认的漏洞证据。")

        lines += [
            "",
            "## 说明",
            f"本次测试共执行 {len(invocations)} 次工具调用，因 {reason} 而停止。",
            "完整的模型分析报告需在增加轮数后重新运行获取。",
        ]
        return "\n".join(lines)

    def _persist(
        self,
        target: str,
        report_text: str,
        invocations: list[ToolInvocation],
        reason: str,
        elapsed: float,
        *,
        agents_summary: list[dict[str, Any]] | None = None,
    ) -> tuple[Path, Path]:
        """保存报告与执行轨迹。"""
        reports_dir = self.settings.reports_dir
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        report_path = reports_dir / f"report_{stamp}.md"
        report_path.write_text(report_text, encoding="utf-8")

        trace = {
            "target": target,
            "stopped_reason": reason,
            "elapsed_seconds": round(elapsed, 3),
            "model": self.settings.llm.model,
            "tool_invocations": [
                {
                    "name": inv.name,
                    "arguments": inv.arguments,
                    "output": inv.output[:2000],
                }
                for inv in invocations
            ],
            "agents": agents_summary or [],
        }
        trace_path = reports_dir / f"trace_{stamp}.json"
        trace_path.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("报告已保存：%s", report_path)
        logger.info("轨迹已保存：%s", trace_path)
        return report_path, trace_path
