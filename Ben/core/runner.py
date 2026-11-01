"""openai-agents SDK 驱动的 Agent 运行器（对齐 strix 的 core/runner.py + execution.py）。

用 SDK 的 ``Runner.run_streamed()`` + ``RunConfig`` 替代手写的多轮循环。
Agent 使用普通 ``Agent``（非 ``SandboxAgent``），因为 DeepSeek/GLM 端点只支持
chat_completions API，不支持 SandboxAgent 的 hosted tools。

Sandbox 容器由 SDK 的 ``DockerSandboxClient`` + ``SandboxSession`` 管理；
shell 工具（exec_command/write_stdin）由 SDK ``Shell`` capability 绑定
live session 生成（与 strix 同一实现），本地 @function_tool 仍经
``ctx.context`` 读取授权白名单等配置。

流程：
  用户输入目标
    -> 加载预设 Prompt（Jinja2 渲染）
    -> 配置 SDK 全局默认值（configure_sdk_model_defaults）
    -> 创建 SDK Docker sandbox client + session
    -> 构建 Agent（factory.build_agent：SDK Shell capability 绑定 session
       生成 exec_command/write_stdin，外加 load_skill/mcp 本地工具）
    -> 构建 RunConfig（model only，无 sandbox 参数）
    -> Runner.run_streamed() 启动 agent 循环
    -> SDK 自动处理工具调用 / 结果回传 / 多轮对话
    -> 满足结束条件（模型不再调用工具 / 达到轮数上限）
    -> 关闭 sandbox session（清理容器）
    -> 生成并保存测试报告

模块职责清晰：
  - Ben.config.settings      → 读取 .env，配置 SDK 全局默认值
  - Ben.agents.factory       → 构建 Agent（工具 + instructions）
  - Ben.agents.prompt        → Jinja2 渲染系统提示词
  - Ben.tools.<family>.tool → 本地 @function_tool 工具（load_skill/mcp）；
    shell 工具由 SDK Shell capability 生成
  - Ben.core.runner          → 编排：配置→agent→sandbox→RunConfig→Runner→报告
  - SDK sandbox              → Docker 容器生命周期 + exec 命令执行
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from agents import RunConfig, Runner, RunItemStreamEvent
from agents.exceptions import MaxTurnsExceeded
from agents.items import (
    ItemHelpers,
    MessageOutputItem,
    RunItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from agents.model_settings import ModelSettings
from agents.sandbox.manifest import Environment, Manifest
from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

from Ben.agents.factory import build_agent
from Ben.agents.prompt import load_system_prompt
from Ben.config import Settings, configure_sdk_model_defaults
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
    """SDK Runner 驱动的 Agent 运行器。

    模块协作流程：
      1. Ben.config.settings → 读取 .env，校验配置
      2. configure_sdk_model_defaults → 注入 API Key/Base URL/chat_completions
      3. Ben.agents.prompt → Jinja2 渲染系统提示词
      4. Ben.agents.factory → 构建 Agent（4 个 @function_tool，无 hosted tools）
      5. SDK DockerSandboxClient → 创建容器 sandbox session（供工具使用）
      6. RunConfig(model only) → SDK Runner.run_streamed
      7. SDK 自动多轮：模型决策→工具调用→结果回传→下一轮
      8. 关闭 sandbox session（清理容器）
      9. 保存 report.md + trace.json
    """

    def __init__(
        self,
        *,
        settings: Settings,
    ) -> None:
        self.settings = settings

    def _load_prompt(self, target: str) -> str:
        """渲染系统提示词（Ben.agents.prompt 模块）。"""
        return load_system_prompt(
            self.settings.prompts_dir,
            settings=self.settings,
            target=target,
        )

    def _build_context(self, target: str) -> dict:
        """构建工具运行时上下文（注入到 ctx.context）。

        工具通过 ctx.context 获取：
          - allowed_targets: 授权白名单
          - command_timeout: 命令超时
          - target: 测试目标

        shell 工具（exec_command/write_stdin）不再读 ctx.context——
        它们在构建 Agent 时已由 Shell capability 绑定 live sandbox session。
        """
        return {
            "allowed_targets": self.settings.allowed_targets,
            "command_timeout": self.settings.command_timeout,
            "target": target,
        }

    def run(self, target: str) -> AgentRunResult:
        """同步入口：执行完整 Agent 任务。"""
        # run() 是同步入口，负责启动 run_async()  ；
        return asyncio.run(self.run_async(target))

    async def run_async(self, target: str) -> AgentRunResult:
        """异步执行 Agent 任务（使用 SDK Runner + SDK Sandbox）。

        这是整个项目的核心编排函数，串联所有模块。
        """
        started = time.monotonic()

        # === 阶段 1: 校验目标 ===
        logger.info("=" * 60)
        logger.info("阶段 1/6: 校验目标")
        logger.info("=" * 60)
        if not target:
            target = self.settings.allowed_targets[0]
            logger.info("未指定目标，使用白名单首项：%s", target)
        _check_target_allowed(target, self.settings.allowed_targets)
        logger.info("目标校验通过：%s", target)

        # === 阶段 2: 配置 SDK 全局默认值 ===
        logger.info("=" * 60)
        logger.info("阶段 2/6: 配置 openai-agents SDK（Ben.config.settings）")
        logger.info("=" * 60)
        configure_sdk_model_defaults(self.settings)
        logger.info("SDK 配置完成：model=%s, api_route=chat_completions", self.settings.llm.model)

        # === 阶段 3: 渲染系统提示词 ===
        logger.info("=" * 60)
        logger.info("阶段 3/6: 渲染系统提示词（Ben.agents.prompt）")
        logger.info("=" * 60)
        system_prompt = self._load_prompt(target)
        logger.info("系统提示词已渲染（%d 字符）", len(system_prompt))

        # === 阶段 4: 创建 SDK Docker Sandbox（含 Caido 代理 sidecar） ===
        logger.info("=" * 60)
        logger.info("阶段 4/6: 创建 Docker Sandbox（BenDockerSandboxClient + Caido sidecar）")
        logger.info("=" * 60)
        import docker as docker_sdk
        # 使用Python Docker SDK连接Docker Engine
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
            image=self.settings.executor_image,
            exposed_ports=(CONTAINER_CAIDO_PORT,),
        )
        logger.info(
            "创建 sandbox session（image=%s, caido_port=%d）...",
            self.settings.executor_image,
            CONTAINER_CAIDO_PORT,
        )
        sandbox_session = await sandbox_client.create(
            options=sandbox_opts, manifest=sandbox_manifest
        )

        # === 阶段 5: 构建 Agent（SDK Shell capability 绑定 live session） ===
        logger.info("=" * 60)
        logger.info("阶段 5/6: 构建 Agent（Ben.agents.factory，shell 工具绑定 session）")
        logger.info("=" * 60)
        agent = build_agent(
            instructions=system_prompt,
            sandbox_session=sandbox_session,
            allowed_targets=self.settings.allowed_targets,
        )
        logger.info(
            "Agent 已构建：%s, tools=%s",
            agent.name,
            [t.name for t in agent.tools],
        )

        # 初始化任务运行变量
        max_turns = self.settings.max_agent_turns
        stopped_reason = "未知"
        final_text = ""
        invocations: list[ToolInvocation] = []
        turns_used = max_turns
        caido_handle: CaidoBootstrapHandle | None = None

        try:
            async with sandbox_session:
                logger.info("Sandbox session 已启动，容器就绪")

                # Caido bootstrap 与 runner 并发：解析宿主机侧映射端口，
                # 后台任务拿 guest token + 建临时项目；proxy 工具首次调用时
                # 才经 handle.get() 等待结果（strix 的 CaidoBootstrapHandle 模式）。
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

                # 构建运行时上下文（proxy 工具经 ctx.context["caido_client"] 取句柄）
                context = self._build_context(target)
                context["caido_client"] = caido_handle

                # === 阶段 6: 启动 SDK Runner ===
                logger.info("=" * 60)
                logger.info(
                    "阶段 6/6: 启动 SDK Runner.run_streamed（model=%s, max_turns=%d）",
                    self.settings.llm.model, max_turns,
                )
                logger.info("=" * 60)

                model_settings = ModelSettings(
                    timeout=self.settings.llm_timeout,
                )
                run_config = RunConfig(
                    model=self.settings.llm.model,
                    model_settings=model_settings,
                    trace_include_sensitive_data=False,
                    tool_not_found_behavior="return_error_to_model",
                )

                # 初始输入
                initial_input = (
                    f"请对以下已授权目标开展测试：{target}\n"
                    f"授权范围：{', '.join(self.settings.allowed_targets)}\n"
                    "请按预设流程，先理解目标，再选择合适的工具取证，"
                    "最后输出有真实证据支持的测试报告。"
                )

                """
                @最关键   Runner是 Agents SDK提供的执行器
                # Runner 将 initial_input发给模型，模型分析任务决定调用工具，SDK执行工具
                # 工具结果返回给模型，模型继续分析，直到完成任务
                """
                stream = Runner.run_streamed(
                    agent,
                    input=initial_input,
                    run_config=run_config,
                    context=context,
                    max_turns=max_turns,
                )

                # 收集所有 run items + 流式日志
                # SDK 在达到 max_turns 时会从 stream_events() 抛出 MaxTurnsExceeded，
                # 但此时 stream.new_items 已包含已完成的工具调用与模型消息。
                # 初始化统计变量
                turn_count = 0
                tool_call_count = 0
                prev_item_type = None
                try:
                    # 遍历异步时间，
                    async for event in stream.stream_events():
                        if isinstance(event, RunItemStreamEvent):
                            item = event.item
                            item_type = type(item).__name__

                            if isinstance(item, ToolCallItem):
                                # 新的模型轮次：当上一个 item 不是 ToolCallItem 时
                                if prev_item_type != "ToolCallItem":
                                    turn_count += 1
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
                                    "[Turn %d/%d] 工具调用: %s → %s",
                                    turn_count, max_turns, tool_name or "?",
                                    _short_args(getattr(raw, "arguments", "") or ""),
                                )
                                tool_call_count += 1

                            elif isinstance(item, ToolCallOutputItem):
                                logger.info("[Turn %d/%d] 工具结果返回", turn_count, max_turns)

                            elif isinstance(item, MessageOutputItem):
                                logger.info("[Turn %d/%d] 模型输出文本消息", turn_count, max_turns)

                            else:
                                logger.debug("[Runner 事件] RunItem: %s", item_type)

                            prev_item_type = item_type

                        elif type(event).__name__ == "AgentUpdatedStreamEvent":
                            logger.debug("[Runner 事件] Agent 更新")
                        else:
                            logger.debug("[Runner 事件] %s", type(event).__name__)

                except MaxTurnsExceeded:
                    logger.info("[Runner] 达到最大轮数（%d），正常停止", max_turns)
                    stopped_reason = f"达到最大轮数（{max_turns} 轮）"
                except Exception as stream_exc:
                    logger.error("Runner 流式事件异常：%s", stream_exc, exc_info=True)
                    stopped_reason = f"Runner 异常：{stream_exc}"

                # 从 stream 提取所有已收集的 items（即使 MaxTurnsExceeded 也能取到）
                items: list[RunItem] = []
                if hasattr(stream, "new_items"):
                    items = list(stream.new_items)

                # 如果还没设置停止原因，检查是否有其他异常
                if stopped_reason == "未知":
                    if stream.run_loop_exception is not None:
                        exc = stream.run_loop_exception
                        logger.error("Runner 异常：%s", exc)
                        stopped_reason = f"Runner 异常：{exc}"
                    else:
                        stopped_reason = "模型结束（输出最终报告）"

                invocations = _extract_tool_invocations(items)
                final_text = _extract_final_text(items)
                turns_used = max(turn_count, 1)
                logger.info(
                    "[Runner] 共 %d 轮，%d 次工具调用，%d 条 items",
                    turns_used, len(invocations), len(items),
                )

        except Exception as exc:
            logger.error("Agent 运行失败：%s", exc, exc_info=True)
            stopped_reason = f"运行错误：{exc}"
            final_text = self._generate_report(target, invocations, stopped_reason)

        finally:
            # 先收尾 Caido（取消在途 bootstrap 或关闭 client），再关 session
            if caido_handle is not None:
                try:
                    await caido_handle.aclose()
                    logger.info("Caido client 已关闭")
                except Exception:
                    pass
            # 关闭 sandbox session（自动清理容器）
            try:
                await sandbox_session.aclose()
                logger.info("Sandbox session 已关闭，容器已清理")
            except Exception:
                pass

        elapsed = time.monotonic() - started
        logger.info("=" * 60)
        logger.info(
            "循环结束（%s），共 %d 次工具调用，耗时 %.1fs",
            stopped_reason, len(invocations), elapsed,
        )
        logger.info("=" * 60)

        if not final_text:
            final_text = self._generate_report(target, invocations, stopped_reason)

        report_path, trace_path = self._persist(
            target, final_text, invocations, stopped_reason, elapsed
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
                        url_info = f" → {args['url']}"
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
                        status = out.get("status_code")
                        if status and status == 200:
                            pass  # 正常响应
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
        }
        trace_path = reports_dir / f"trace_{stamp}.json"
        trace_path.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("报告已保存：%s", report_path)
        logger.info("轨迹已保存：%s", trace_path)
        return report_path, trace_path
