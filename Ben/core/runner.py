"""多 Agent 编排运行器（结构与流程对齐 strix 的 core/runner.py）。

用 ``AgentCoordinator`` + ``run_agent_loop``（Ben.core.execution，与 strix 同一实现）
驱动 root agent，root 可经 ``create_agent`` 图工具并行派生子 agent；
子 agent 完成时经 ``agent_finish`` 把完成报告投递到父 inbox。

流程（对齐 strix run_strix_scan）：
  校验目标（授权白名单，Ben 特有）
  -> 配置 SDK 全局默认值（configure_sdk_model_defaults）
  -> 创建 ReportState + AgentCoordinator（agents.json/agents.db 写入
     per-run 目录 strix_runs/<run>/.state，resume 时复用同一目录）
  -> 水合 todos/notes/coverage 磁盘状态（同一 .state 目录）
  -> resume 时 coordinator.restore(agents.json) 并找回 root_id
  -> 创建 SDK Docker sandbox（session_manager 延迟启动 + Caido sidecar）
  -> 配置 output_store spill writer（写沙箱 /workspace/.tool-output/）
  -> Caido bootstrap（后台并发，proxy 工具首次调用时才等待）
  -> 构建 root Agent（factory.build_agent，is_root=True，挂 finish_scan）
  -> make_child_factory + spawn_child_agent 闭包注入 ctx.context
  -> open_agent_session（SQLite agents.db，root/child 各一条会话）
  -> resume 时 respawn_subagents 拉起未终结子 agent，initial_input=[]
  -> run_agent_loop（SDK Runner.run_streamed 多轮循环 + 生命周期回收）
  -> 收尾：cancel_descendants -> 关 session -> 关 Caido -> 关 sandbox
  -> 生成并保存 report.md / trace.json

与 strix 的差异（Ben 架构约束，刻意保留）：
- 普通 ``Agent`` + 手动绑定 Shell capability（chat_completions 路由，
  不用 SandboxAgent/hosted tools）；RunConfig 不带 SandboxRunConfig。
- 默认非交互自主运行；--interactive 时挂 wait_for_user 并可经消息唤醒，
  -r/--resume 时从 per-run 目录的 agents.json/agents.db 断点续扫。
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
from Ben.agents.factory import build_agent, make_child_factory
from Ben.agents.prompt import build_system_prompt_context
from Ben.config import Settings, configure_sdk_model_defaults
from Ben.config.models import (
    BenProvider,
    model_supports_images,
    supports_strict_tool_schemas,
)
from Ben.core.agents import AgentCoordinator
from Ben.core.execution import respawn_subagents, run_agent_loop
from Ben.core.execution import spawn_child_agent as start_child_agent
from Ben.core.paths import runtime_state_dir
from Ben.core.hooks import (
    SUBAGENT_TURN_LIMITS_KEY,
    BudgetExceededError,
    ReportUsageHooks,
)
from Ben.core.inputs import build_root_task, build_scan_targets, make_model_settings
from Ben.core.sessions import open_agent_session
from Ben.interface.targets import HOST_GATEWAY_HOSTNAME, is_whitebox_scan
from Ben.llm import request_log
from Ben.runtime import session_manager
from Ben.runtime.caido import CaidoBootstrapHandle

logger = logging.getLogger(__name__)


# prompt 渲染前等待 MCP 连接 + 工具目录就绪的超时（对齐 strix runner；
# 超时后照样开跑，目录未就绪的连接在 prompt 里显示为 connecting）。
_MCP_PROMPT_WARMUP_TIMEOUT_SECONDS = 30.0


def _mcp_roster_payload(registry: "McpRegistry") -> list[dict[str, Any]]:
    """本次运行的 MCP 花名册，非机密生命周期状态字典（对齐 strix）。"""
    return [
        {
            "name": status.name,
            "provider": status.provider,
            "tool_count": status.tool_count,
            "dead": status.dead,
            "state": status.state,
        }
        for status in registry.statuses()
    ]


def _mcp_prompt_roster(registry: "McpRegistry") -> list[dict[str, Any]]:
    """渲染进 agent prompt 的 MCP 花名册（仅 catalog_ready 才带 tool_count）。"""
    return [
        {
            "name": summary.name,
            "purpose": summary.purpose,
            "tool_count": summary.tool_count if summary.state == "catalog_ready" else None,
            "state": summary.state,
        }
        for summary in registry.summaries()
    ]


def _record_mcp_connections(connection_names: list[str]) -> None:
    """记录本次运行配置了哪些 MCP 服务器，供报告/事后检查（对齐 strix）。"""
    from Ben.report import get_global_report_state

    report_state = get_global_report_state()
    if report_state is None:
        return
    report_state.record_mcp_connections(connection_names)


def _persist_mcp_status(roster: list[dict[str, Any]]) -> None:
    """把非机密的 MCP 连接状态花名册写进 run.json（对齐 strix）。"""
    from Ben.report import get_global_report_state

    report_state = get_global_report_state()
    if report_state is None:
        return
    report_state.record_mcp_connection_status(roster)


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
    # localhost 目标在 build_targets_info 阶段已改写为 host-gateway 主机名
    # （容器内回指宿主机）；比较白名单时归一化回 localhost，授权语义不变。
    hostname = parts.hostname
    if hostname == HOST_GATEWAY_HOSTNAME:
        hostname = "localhost"
    origin = (
        f"{parts.scheme}://{hostname}:{parts.port}"
        if parts.port is not None
        else f"{parts.scheme}://{hostname}"
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


def _warn_public_whitelist(allowed: tuple[str, ...]) -> None:
    """白名单含公网地址时打告警：公网目标需额外确认授权凭证，防误配。"""
    import ipaddress

    for item in allowed:
        host = urlsplit(item).hostname
        if not host:
            continue
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue  # 域名无法离线判定公私网，跳过
        if ip.is_global:
            logger.warning(
                "授权白名单包含公网地址 %s：请确认持有该目标的书面授权，"
                "公网测试风险自负", item,
            )


def _check_targets_allowed(
    targets_info: list[dict[str, Any]], allowed: tuple[str, ...]
) -> None:
    """多目标授权校验：web/ip 目标逐一过白名单；源码类目标（白盒）跳过。

    域名目标在 infer_target_type 阶段已归一为 https://<domain> 的
    web_application，因此域名校验复用 web 路径。
    """
    _warn_public_whitelist(allowed)
    for target_info in targets_info:
        target_type = target_info.get("type")
        details = target_info.get("details") or {}
        if target_type == "web_application":
            _check_target_allowed(details.get("target_url", ""), allowed)
        elif target_type == "ip_address":
            target_ip = details.get("target_ip", "")
            if target_ip == HOST_GATEWAY_HOSTNAME:
                target_ip = "localhost"
            allowed_hosts = {
                urlsplit(item).hostname for item in allowed if urlsplit(item).hostname
            }
            if target_ip not in allowed_hosts:
                raise ValueError(
                    f"目标 IP {target_ip} 不在授权白名单 {sorted(allowed_hosts)} 内，拒绝启动"
                )
        # repository / local_code：源码目标（白盒），跳过白名单校验。


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
      4. session_manager.create_or_reuse → 容器 sandbox session（Caido sidecar，defer 后台启动）
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
        local_sources: list[dict] | None = None,
        targets_info: list[dict] | None = None,
        scan_mode: str = "blackbox",
        user_instructions: str | None = None,
        diff_scope: dict | None = None,
        interactive: bool = False,
        resume_run_name: str | None = None,
        resume_instruction: str | None = None,
    ) -> None:
        self.settings = settings
        # 白盒源码挂载（session_manager.build_bind_mounts 的输入）；
        # 空列表 = 纯黑盒，行为与原先一致。
        self.local_sources = local_sources or []
        # 多目标描述（Ben.interface.targets.build_targets_info 的输出）；
        # None 时 run/run_async 回退为单 web 目标（向后兼容）。
        self.targets_info = targets_info
        # CLI --scan-mode（quick/standard/deep），记入 scan_config/run 记录；
        # agent 片段选择用 blackbox/whitebox（Ben 既有约定），与该值解耦。
        self.scan_mode = scan_mode
        self.user_instructions = user_instructions
        # diff-scope 元数据（resolve_diff_scope_context(...).metadata）；
        # {"active": True} 时 root task 附带变更文件范围约束。
        self.diff_scope = diff_scope
        # --interactive：root/child 挂 wait_for_user，终态后 park 等待消息唤醒。
        self.interactive = interactive
        # -r/--resume 的 run 名称（strix_runs/<name>）；None = fresh run。
        self.resume_run_name = resume_run_name
        # resume 时用户新给的 --instruction，经 coordinator.send 注入 root
        # 会话（对齐 strix runner 的 resume_instruction 注入）。
        self.resume_instruction = resume_instruction

    def run(self, target: str | None = None) -> AgentRunResult:
        """同步入口：执行完整 Agent 任务。

        ``targets_info`` 已给出时忽略 ``target``；否则 ``target`` 为单一
        web 目标（缺省回退白名单首项），行为与多目标改造前一致。
        """
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

    async def run_async(self, target: str | None = None) -> AgentRunResult:
        """异步执行多 Agent 编排任务（核心编排函数，串联所有模块）。"""
        started = time.monotonic()
        settings = self.settings

        # === 阶段 1: 校验目标（Ben 特有：授权白名单） ===
        logger.info("=" * 60)
        logger.info("阶段 1/6: 校验目标")
        logger.info("=" * 60)
        targets_info = self.targets_info
        if not targets_info:
            # 单目标回退路径：与多目标改造前的行为完全一致。
            if not target:
                target = settings.allowed_targets[0]
                logger.info("未指定目标，使用白名单首项：%s", target)
            targets_info = [
                {
                    "type": "web_application",
                    "details": {"target_url": target},
                    "original": target,
                }
            ]
        _check_targets_allowed(targets_info, settings.allowed_targets)
        # 主目标展示名：第一个 web 目标，否则 targets_info[0] 的 original。
        primary = next(
            (t for t in targets_info if t.get("type") == "web_application"),
            targets_info[0],
        )
        target = str(primary.get("original") or target or "")
        is_whitebox = is_whitebox_scan(targets_info)
        is_diff_scoped = bool(
            isinstance(self.diff_scope, dict) and self.diff_scope.get("active")
        )
        logger.info(
            "目标校验通过：%d 个目标（主目标 %s，whitebox=%s，diff_scope=%s）",
            len(targets_info), target, is_whitebox, is_diff_scoped,
        )

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

        # === 阶段 3: 创建 ReportState + AgentCoordinator + 水合磁盘状态 ===
        # 对齐 strix core/runner.py：agents.json 快照 + SQLite agents.db 落在
        # per-run 目录（strix_runs/<run_name>/.state/）。resume 时复用被恢复
        # run 的目录；fresh run 的 run_name 带时间戳 + root_id，天然新目录，
        # 两个 fresh run 不会共享快照。
        logger.info("=" * 60)
        logger.info("阶段 3/6: 创建 AgentCoordinator（多 agent 图状态）")
        logger.info("=" * 60)
        is_resume = self.resume_run_name is not None
        root_id: str | None
        if is_resume:
            run_name = str(self.resume_run_name)
            root_id = None  # restore 后从快照的 parent=None 记录找回
        else:
            root_id = uuid.uuid4().hex[:8]
            run_name = f"ben_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{root_id}"

        # 报告状态（对齐 strix run_scan）：finish_scan / agent_finish /
        # coverage / SARIF 都经模块级 ReportState 取扫描产物；缺了它这些写入
        # 全部空转（finish_scan 会回 "not persisted"，子 agent 的发现也归档不了）。
        # resume 时 hydrate_from_run_dir 恢复 run.json 与已归档漏洞（fresh 幂等）。
        from Ben.report import ReportState, set_global_report_state

        report_state = ReportState(run_name=run_name)
        report_state.hydrate_from_run_dir()
        set_global_report_state(report_state)
        run_dir = report_state.get_run_dir()
        state_dir = runtime_state_dir(run_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        agents_path = state_dir / "agents.json"
        agents_db = state_dir / "agents.db"
        logger.info(
            "ReportState 已初始化：run=%s，产物目录=%s（%s）",
            report_state.run_id,
            run_dir,
            "resume" if is_resume else "fresh",
        )

        coordinator = AgentCoordinator()
        coordinator.set_snapshot_path(agents_path)
        coordinator.root_finish_tool = "finish_scan"

        from Ben.tools.coverage.tools import hydrate_coverage_from_disk
        from Ben.tools.notes.tools import hydrate_notes_from_disk
        from Ben.tools.threat_model.tools import hydrate_threat_models_from_disk
        from Ben.tools.todo.tools import hydrate_todos_from_disk

        hydrate_todos_from_disk(state_dir)
        hydrate_notes_from_disk(state_dir)
        hydrate_coverage_from_disk(state_dir)
        hydrate_threat_models_from_disk(state_dir)

        # resume：从 agents.json 恢复快照并找回 root（对齐 strix runner
        # L341-383；agents.db 缺失说明会话历史已丢，无法续跑）。
        if is_resume:
            if not agents_path.exists():
                raise RuntimeError(
                    f"无法恢复 run {run_name}：缺少 {agents_path}"
                    "（该 run 未到首个快照，无可恢复状态）"
                )
            try:
                snap = json.loads(agents_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"无法恢复 run {run_name}：agents.json 不可读：{exc}"
                ) from exc
            if not agents_db.exists():
                raise RuntimeError(
                    f"无法恢复 run {run_name}：缺少 SDK 会话数据库 {agents_db}"
                )
            await coordinator.restore(snap)
            for aid, parent in coordinator.parent_of.items():
                if parent is None:
                    root_id = aid
                    break
            if root_id is None:
                raise RuntimeError(
                    f"无法恢复 run {run_name}：agents.json 中没有 root agent（parent=None）"
                )
            logger.info(
                "Resume: 已恢复 coordinator（%d 个 agent）；root=%s",
                len(coordinator.statuses),
                root_id,
            )

        # === 阶段 4: 创建 SDK Docker Sandbox（含 Caido 代理 sidecar） ===
        # 对齐 strix runtime/session_manager.create_or_reuse：backend 注册表
        # （BEN_RUNTIME_BACKEND）选实现，defer=True 时容器在后台并行启动，
        # DeferredSandboxSession 的 start() 为 no-op（``async with`` 安全），
        # 首次真实调用（exec/write/resolve_exposed_port）时才等待就绪；
        # Caido bootstrap 同并发（bundle["caido_client"] 即 CaidoBootstrapHandle）。
        logger.info("=" * 60)
        logger.info("阶段 4/6: 创建 Docker Sandbox（session_manager 延迟启动 + Caido sidecar）")
        logger.info("=" * 60)
        scan_id = uuid.uuid4().hex[:8]
        sandbox_bundle = await session_manager.create_or_reuse(
            scan_id,
            image=settings.executor_image,
            local_sources=self.local_sources,
            status_sink=lambda phase: logger.info("[sandbox] %s", phase),
            defer=True,
        )
        sandbox_session = sandbox_bundle["session"]

        # === 阶段 5: 构建 root Agent + 子 agent 工厂 ===
        logger.info("=" * 60)
        logger.info("阶段 5/6: 构建 Agent（Ben.agents.factory，root 挂 finish_scan）")
        logger.info("=" * 60)

        max_turns = settings.max_agent_turns
        stopped_reason = "未知"
        final_text = ""
        invocations: list[ToolInvocation] = []
        turns_used = max_turns
        # 提取逻辑在 finally 之后统一执行（成功/异常同路径），这些名字
        # 必须预绑定，否则 try 早期异常会让 finally 后的代码 NameError。
        result = None
        turn_counts: dict[str, int] = {}
        run_error: BaseException | None = None
        # Caido bootstrap 与 sandbox bring-up 并发（strix 的
        # CaidoBootstrapHandle 模式）：proxy 工具首次调用时才经 handle.get()
        # 等待结果；由 session_manager 在 bundle 中一并给出。
        caido_handle: CaidoBootstrapHandle | None = sandbox_bundle["caido_client"]
        sessions_to_close: list = []

        # strix 形态的 scan_config：多 targets + scan_mode + 白盒源码挂载 +
        # diff-scope 元数据；root_task 由 build_root_task 据此生成。
        scan_config: dict = {
            "targets": targets_info,
            "scan_mode": self.scan_mode,
            "user_instructions": (
                self.user_instructions
                or f"授权范围：{', '.join(settings.allowed_targets)}。"
                "请按预设流程，先理解目标，再选择合适的工具取证，"
                "最后输出有真实证据支持的测试报告。"
            ),
            "local_sources": self.local_sources,
            "diff_scope": self.diff_scope or {"active": False},
            "is_whitebox": is_whitebox,
            # resume 时用户新给的 --instruction，稍后经 coordinator.send 注入
            # root 会话（对齐 strix runner L631-645）；fresh run 恒为空串。
            "resume_instruction": self.resume_instruction or "",
        }
        root_task = build_root_task(scan_config)
        prompt_context = build_system_prompt_context(settings, target)

        report_state.set_scan_config(scan_config)

        # === 阶段 5 前置: 装配本次运行的 MCP 连接（对齐 strix runner） ===
        # 连接来源：~/.ben/mcp-servers.json（BEN_MCP_CONFIG 覆盖，
        # BEN_MCP_ONLY / BEN_MCP_EXCLUDE 做运行级选择）。不向 agent 注册任何
        # 静态工具：每个 agent 经 list_mcps / describe_mcp / call_mcp 等
        # 五个发现/派发工具按需触达，prompt 由 mcp_available/mcp_connections
        # 给出简短静态指引。失败开放：配置缺失或服务器连不上都不打断扫描。
        from Ben.tools.mcp import McpConnectionRequest, McpRegistry, load_mcp_configs

        mcp_registry = McpRegistry()
        try:
            mcp_requests = [
                McpConnectionRequest(config=config) for config in load_mcp_configs()
            ]
            if mcp_requests:
                for request in mcp_requests:
                    mcp_registry.register(request)
                _record_mcp_connections(mcp_registry.names())
                logger.info(
                    "MCP: 已配置 %d 个连接，开始连接并拉取工具目录",
                    len(mcp_registry),
                )
                prompt_context["mcp_available"] = True
                prompt_context["mcp_connections"] = _mcp_prompt_roster(mcp_registry)

                def _emit_mcp_status() -> None:
                    prompt_context["mcp_connections"] = _mcp_prompt_roster(mcp_registry)
                    _persist_mcp_status(_mcp_roster_payload(mcp_registry))

                mcp_registry.set_status_sink(_emit_mcp_status)
                _emit_mcp_status()
                warmup_task = mcp_registry.start_warmup(max_concurrency=6)
                await asyncio.wait(
                    {warmup_task},
                    timeout=_MCP_PROMPT_WARMUP_TIMEOUT_SECONDS,
                )
        except Exception:
            logger.exception("配置 MCP 服务器失败；本次运行不带 MCP 继续")

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
                    interactive=self.interactive,
                    record_tool_timings=settings.telemetry.run_record_timings,
                )

                root_agent = build_agent(
                    name="Root Agent",
                    skills=[],
                    is_root=True,
                    sandbox_session=sandbox_session,
                    allowed_targets=settings.allowed_targets,
                    scan_mode="blackbox",
                    is_whitebox=is_whitebox,
                    is_diff_scoped=is_diff_scoped,
                    interactive=self.interactive,
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

                if not is_resume:
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
                    is_whitebox=is_whitebox,
                    is_diff_scoped=is_diff_scoped,
                    interactive=self.interactive,
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
                        interactive=self.interactive,
                        hooks=hooks,
                        **kwargs,
                    )

                # 运行时上下文（图工具/notes/todo/coverage 经 ctx.context 取
                # coordinator 与身份；proxy 工具取 caido_client；MCP 派发
                # 工具取 mcp_registry）
                context: dict = {
                    "coordinator": coordinator,
                    "sandbox_session": sandbox_session,
                    "caido_client": caido_handle,
                    "mcp_registry": mcp_registry,
                    "agent_id": root_id,
                    "parent_id": None,
                    "interactive": self.interactive,
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

                # resume：拉起快照里未终结的子 agent（对齐 strix runner
                # L609-622）；root 自身由下面的 run_agent_loop 驱动。
                if is_resume:
                    await respawn_subagents(
                        coordinator=coordinator,
                        factory=child_agent_builder,
                        agents_db_path=agents_db,
                        sessions_to_close=sessions_to_close,
                        run_config=run_config,
                        max_turns=max_turns,
                        interactive=self.interactive,
                        parent_ctx=context,
                        root_id=root_id,
                        event_sink=event_sink,
                        hooks=hooks,
                    )

                # resume 时 SDK 从 agents.db 回放 root 历史，不再重投 root_task。
                initial_input: Any = [] if is_resume else root_task

                # Resume + 新 --instruction：SDK 回放驱动 root（initial_input=[]），
                # 新 instruction 否则会被静默忽略；注入为 root 会话里的新用户
                # 消息（对齐 strix runner L626-645）。
                resume_instruction = str(scan_config.get("resume_instruction") or "").strip()
                if is_resume and resume_instruction:
                    await coordinator.send(
                        root_id,
                        {
                            "from": "user",
                            "type": "instruction",
                            "priority": "high",
                            "content": resume_instruction,
                        },
                    )
                    logger.info(
                        "Resume: 注入新 instruction 到 root SDK session（len=%d）",
                        len(resume_instruction),
                    )

                async with coordinator._lock:
                    root_status = coordinator.statuses.get(root_id)

                try:
                    result = await run_agent_loop(
                        agent=root_agent,
                        initial_input=initial_input,
                        run_config=run_config,
                        context=context,
                        max_turns=max_turns,
                        coordinator=coordinator,
                        agent_id=root_id,
                        interactive=self.interactive,
                        session=root_session,
                        start_parked=bool(
                            self.interactive and is_resume and root_status != "running"
                        ),
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
                        # run_agent_loop 正常返回但 root 已被标记崩溃/失败，
                        # 同样属于异常终止，报告不得输出"未发现漏洞"结论。
                        run_error = run_error or RuntimeError(stopped_reason)
                    else:
                        stopped_reason = f"运行结束（root 状态：{root_status}）"

        except BudgetExceededError as exc:
            logger.info("扫描达到预算上限，停止：%s", exc)
            stopped_reason = f"预算上限：{exc}"
            with contextlib.suppress(Exception):
                await coordinator.set_status(root_id, "stopped")
        except Exception as exc:
            logger.error("Agent 运行失败：%s", exc, exc_info=True)
            run_error = exc
            # failure_text 对 str(exc) 为空串的异常（如 httpx.ReadTimeout）
            # 回退到类型名，保证终止原因永远可读。
            stopped_reason = f"运行错误：{request_log.failure_text(exc)}"
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
                await mcp_registry.close()
            with contextlib.suppress(Exception):
                await coordinator._maybe_snapshot()
            # 对齐 strix 收尾：session_manager.cleanup 负责 Caido 关闭 +
            # client.delete 删容器 + docker client 关闭（best-effort，
            # 不让清理失败阻断下一次扫描）。async with 退出时的 aclose()
            # 只做 session 级收尾，不删容器。
            try:
                await session_manager.cleanup(scan_id)
                logger.info("Sandbox session 已关闭，容器已清理")
            except Exception:
                pass
            from Ben.tools.output_store import configure_spill_writer

            configure_spill_writer(None)

        # 从 root 的 run 结果提取 items（成功/异常同路径，原先位于 try 内
        # await 之后，异常时整段被跳过，导致 tool_invocations 恒为空）。
        # 异常时 result 为 None，但完整会话已落 agents.db，session 路径
        # 是事实来源，不依赖 result 对象。
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

        # len(invocations) 与 agents 汇总的 tool_calls 应互相印证；
        # 不一致说明提取路径有问题，给出告警而不是静默落盘。
        if not invocations:
            logger.warning(
                "root 工具调用记录为 0（run_error=%s）；"
                "请对照 trace.json 中 agents 汇总的 tool_calls 检查提取链路",
                run_error is not None,
            )

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
                status = "failed" if run_error is not None else "stopped"
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
            final_text = self._generate_report(
                target,
                invocations,
                stopped_reason,
                terminated_ok=run_error is None,
                turns_used=turns_used,
            )

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
        target: str,
        invocations: list[ToolInvocation],
        reason: str,
        *,
        terminated_ok: bool,
        turns_used: int = 0,
    ) -> str:
        """基于真实执行记录生成测试报告（模型未输出最终报告时使用）。

        ``terminated_ok=False``（异常中断）时禁止输出"未发现漏洞"式结论：
        一次基础设施故障不能被渲染成一次干净的安全结论。
        """
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
            if terminated_ok:
                lines.append("- 无成功执行的工具调用（测试未实际开始）")
            else:
                lines.append(
                    "- 工具调用记录缺失或不完整：运行异常中断，"
                    "实际调用次数以 trace.json 的 agents 汇总为准"
                )
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
            if not terminated_ok:
                lines.append("")
                lines.append(
                    f"> 注意：以上为中断前收集的证据。运行因「{reason}」异常中断，"
                    "覆盖不完整，不能据此判定目标不存在其他漏洞。"
                )
        elif terminated_ok:
            lines.append("- 本次测试未发现可确认的漏洞证据。")
        else:
            lines.append(
                f"- **本次测试未完成，结论无效**：运行因「{reason}」异常中断，"
                f"中断前已记录 {len(invocations)} 次工具调用。"
                "此处未列出证据不代表目标无漏洞，请修复中断原因后重新运行。"
            )

        if terminated_ok:
            lines += [
                "",
                "## 说明",
                f"本次测试共执行 {len(invocations)} 次工具调用，因 {reason} 而停止。",
                "完整的模型分析报告需在增加轮数后重新运行获取。",
            ]
        else:
            lines += [
                "",
                "## 说明",
                f"本次测试因「{reason}」异常中断（约 {turns_used} 轮），"
                f"共记录 {len(invocations)} 次工具调用，结论无效。",
                "完整的模型分析报告请在修复中断原因后重新运行获取。",
            ]
        return "\n".join(lines)

    @staticmethod
    def _archive_old_reports(reports_dir: Path, keep_stamp: str) -> None:
        """把本次之前的 report_*/trace_* 按月份归档到 reports/archive/YYYYMM/。

        时间戳前缀（YYYYMMDD_HHMMSS）决定归档月份；本次产出的两个文件
        （keep_stamp 前缀）保留在 reports/ 根部便于查看最新结果。
        """
        archive_root = reports_dir / "archive"
        for path in reports_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if not (name.startswith(("report_", "trace_"))):
                continue
            stamp = name.split("_", 1)[1] if "_" in name else ""
            if not stamp[:8].isdigit() or stamp == keep_stamp:
                continue
            month_dir = archive_root / stamp[:6]
            month_dir.mkdir(parents=True, exist_ok=True)
            try:
                path.replace(month_dir / name)
            except OSError:
                logger.warning("归档失败：%s", path, exc_info=True)

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
        self._archive_old_reports(reports_dir, stamp)

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
