"""最小 Agent 循环运行器。

把用户给定的流程实现为可运行、可调试、可通过日志验证的代码：

  用户输入目标
    -> 加载预设 Prompt（Ben/agents/prompts/vulnerability_testing.txt）
    -> 组装任务上下文
    -> 调用 DeepSeek
    -> 解析响应，判断是否提出工具调用
    -> 校验工具名称与参数（ToolRegistry）
    -> 在 Docker 容器内真实执行
    -> 把真实结果返回模型，进入下一轮
    -> 满足结束条件（模型不再调用工具 / 达到轮数上限）
    -> 生成并保存测试报告

关键边界：模型的"决策"与程序的"实际执行"严格分离。模型提出执行请求只代表
意图；只有 ToolRegistry/执行器返回真实结果，才认定操作发生。模型不能仅凭
措辞让任何命令执行。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from Ben.agents.prompt import load_system_prompt
from Ben.config import Settings
from Ben.llm.deepseek_client import LLMError, LLMResponse
from Ben.runtime.docker_executor import DockerExecutor
from Ben.tools.registry import (
    ToolRegistry,
    ToolRuntime,
    ToolError,
)

logger = logging.getLogger(__name__)

"""
@dataclass的作用就是省去 以下代码
    self.turn = turn
    self.name = name
    self.arguments = arguments
    self.ok = ok
    self.result_summary = result_summary
    self.data = data if data is not None else {}
    self.error = error
"""
@dataclass
class ToolInvocation:
    """一次工具调用的完整记录（决策参数 + 真实结果），用于轨迹与报告。"""
    turn: int
    name: str
    arguments: dict
    ok: bool
    result_summary: str
    data: dict = field(default_factory=dict)
    error: str = ""



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


class AgentRunner:
    """驱动"模型决策 <-> 工具执行"多轮循环的最小运行器。"""
    
    def __init__(self,*,
    # 创建对象必须传入的参数
    settings: Settings,llm: object,registry: ToolRegistry,executor: DockerExecutor,) -> None:
        # 初始化属性
        self.settings = settings
        self.llm = llm  # 满足 LLMClient 协议（DeepSeekClient）
        self.registry = registry
        self.executor = executor
        self.runtime = ToolRuntime(
            executor=executor,
            allowed_targets=settings.allowed_targets,
            command_timeout=settings.command_timeout,
        )

    # ------------------------------------------------------------------
    def _load_prompt(self) -> str:
        return load_system_prompt(self.settings.prompts_dir)
    
    # 无需实例化类，就可以直接通过类名调用
    @staticmethod
    def _check_target_allowed(target: str, allowed: tuple[str, ...]) -> None:
        """
        result = urlsplit("https://example.com:8443/path?id=1")
        print(result)  返回一个SplitResult对象
        SplitResult(
            协议     scheme='https',  
            网络位置 netloc='example.com:8443',
            路径     path='/path',
            查询参数 query='id=1',
            锚点     fragment=''
            )
        """
        parts = urlsplit(target)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ToolError(f"目标必须是 http/https URL：{target!r}")
        # 三元表达式 origin = （值1 if 条件 else 值2）
        # parts.port not None成立则取值1，否则取值2
        origin = (
            f"{parts.scheme}://{parts.hostname}:{parts.port}"
            # 端口
            if parts.port is not None
            else f"{parts.scheme}://{parts.hostname}"
        )
        # 创建空集合，存储允许的URL
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
            raise ToolError(
                f"目标 {origin} 不在授权白名单 {sorted(allowed_origins)} 内，拒绝启动"
            )

    @staticmethod
    def _assistant_message(response: LLMResponse) -> dict:
        """把模型响应重建为可回传的 assistant 消息（含 tool_calls）。"""
        message: dict = {"role": "assistant", "content": response.text or None}
        if response.has_tool_calls:
            message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ]
        return message

    # ------------------------------------------------------------------
    # 传入http://juice-shop:3000，返回AgentRunResult对象
    def run(self, target: str) -> AgentRunResult:
        # 第一阶段 记录任务开始时的时间
        started = time.monotonic()
        self._check_target_allowed(target, self.settings.allowed_targets)

        # 确保执行容器就绪（真实容器在此时启动/复用）。
        self.executor.ensure_container()

        system_prompt = self._load_prompt()
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"请对以下已授权目标开展测试：{target}\n"
                    f"授权范围：{', '.join(self.settings.allowed_targets)}\n"
                    "请按预设流程，先理解目标，再选择合适的工具取证，"
                    "最后输出有真实证据支持的测试报告。"
                ),
            },
        ]

        invocations: list[ToolInvocation] = []
        stopped_reason = "未知"
        final_text = ""
        tool_schemas = self.registry.openai_tools()

        for turn in range(1, self.settings.max_agent_turns + 1):
            logger.info("===== 第 %d/%d 轮决策 =====", turn, self.settings.max_agent_turns)

            # 尚未取得任何真实工具结果前，强制模型调用 http_check，
            # 防止它跳过取证、凭经验直接编造一份"报告"。
            force_tool = len(invocations) == 0
            tool_choice = (
                {"type": "function", "function": {"name": "http_check"}}
                if force_tool
                else "auto"
            )
            try:
                response: LLMResponse = self.llm.chat(
                    messages, tool_schemas, tool_choice=tool_choice
                )
            except LLMError as exc:
                # 模型错误：记录并结束，不伪造报告。
                logger.error("模型调用失败，终止任务：%s", exc)
                stopped_reason = f"模型错误：{exc}"
                final_text = self._fallback_report(target, invocations, stopped_reason)
                break

            messages.append(self._assistant_message(response))

            # 没有工具调用
            if not response.has_tool_calls:
                if force_tool:
                    # 被要求取证却仍输出纯文本：该文本没有真实执行依据，不采信，
                    # 追加纠正消息后继续，直到模型真正调用工具。
                    logger.warning("模型未调用工具就输出结果，已纠正并要求先取证")
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "你还没有调用任何工具，上面的状态码、响应大小、耗时等"
                                "都没有真实执行依据，属于无效内容。请立即调用 http_check "
                                "工具对目标取证；禁止在工具返回真实结果前输出任何数字或"
                                "最终报告。"
                            ),
                        }
                    )
                    continue
                # 已有真实工具结果，模型输出最终报告 -> 结束循环。
                final_text = response.text or "(模型未返回文本)"
                stopped_reason = "模型结束（输出最终报告）"
                logger.info("模型不再调用工具，任务结束")
                break

            # 有工具调用：逐个校验并在容器内真实执行，再把结果回传。
            for tc in response.tool_calls:
                logger.info("模型请求工具：%s 参数：%s", tc.name, tc.arguments)
                try:
                    tool_result = self.registry.execute(tc.name, tc.arguments, self.runtime)
                    invocation = ToolInvocation(
                        turn=turn,
                        name=tc.name,
                        arguments=tc.arguments,
                        ok=tool_result.ok,
                        result_summary=tool_result.summary,
                        data=tool_result.data,
                        error=tool_result.error,
                    )
                    tool_content = tool_result.to_message()
                except ToolError as exc:
                    # 校验/执行失败也要作为 tool 消息回传，让模型据此修正。
                    logger.warning("工具调用被拒绝/失败：%s", exc)
                    invocation = ToolInvocation(
                        turn=turn,
                        name=tc.name,
                        arguments=tc.arguments,
                        ok=False,
                        result_summary="",
                        error=str(exc),
                    )
                    tool_content = (
                        f"[工具 {tc.name} 执行失败]\n{exc}\n请修正参数或选择其它操作，"
                        "不要重复同样的错误请求。"
                    )
                invocations.append(invocation)
                # tool 角色消息必须带对应 tool_call_id
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": tool_content}
                )
        else:
            # 达到轮数上限仍未结束：基于已有真实结果生成兜底报告。
            stopped_reason = f"达到最大轮数 {self.settings.max_agent_turns}"
            logger.warning(stopped_reason)
            final_text = self._fallback_report(target, invocations, stopped_reason)

        elapsed = time.monotonic() - started
        logger.info("循环结束（%s），共 %d 次工具调用，耗时 %.1fs",
                    stopped_reason, len(invocations), elapsed)

        report_path, trace_path = self._persist(
            target, final_text, messages, invocations, stopped_reason, elapsed
        )
        return AgentRunResult(
            target=target,
            report_text=final_text,
            report_path=report_path,
            trace_path=trace_path,
            turns_used=min(turn, self.settings.max_agent_turns),
            stopped_reason=stopped_reason,
            invocations=invocations,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _fallback_report(
        target: str, invocations: list[ToolInvocation], reason: str
    ) -> str:
        """模型异常/超轮时，基于真实执行记录生成兜底报告（不臆造漏洞）。"""
        lines = [
            "# 测试报告（兜底生成）",
            "",
            f"测试目标：{target}",
            f"终止原因：{reason}",
            "",
            "## 实际执行的测试操作",
        ]
        if not invocations:
            lines.append("- 无成功执行的工具调用")
        for inv in invocations:
            status = "成功" if inv.ok else f"失败（{inv.error}）"
            lines.append(f"- 第{inv.turn}轮 {inv.name}({json.dumps(inv.arguments, ensure_ascii=False)}) -> {status}")
        lines += [
            "",
            "## 测试结论",
            "本次未取得可确认漏洞的充分证据；未发现可验证漏洞。",
            "建议在模型服务恢复后重新运行，以完成完整流程。",
        ]
        return "\n".join(lines)

    def _persist(
        self,
        target: str,
        report_text: str,
        messages: list[dict],
        invocations: list[ToolInvocation],
        reason: str,
        elapsed: float,
    ) -> tuple[Path, Path]:
        """保存报告（.md）与执行轨迹（.json），返回路径。"""
        reports_dir = self.settings.reports_dir
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        report_path = reports_dir / f"report_{stamp}.md"
        report_path.write_text(report_text, encoding="utf-8")

        trace = {
            "target": target,
            "stopped_reason": reason,
            "elapsed_seconds": round(elapsed, 3),
            "model": self.settings.deepseek_model,
            "tool_invocations": [
                {
                    "turn": inv.turn,
                    "name": inv.name,
                    "arguments": inv.arguments,
                    "ok": inv.ok,
                    "result_summary": inv.result_summary,
                    "data": inv.data,
                    "error": inv.error,
                }
                for inv in invocations
            ],
            "message_count": len(messages),
        }
        trace_path = reports_dir / f"trace_{stamp}.json"
        trace_path.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("报告已保存：%s", report_path)
        logger.info("轨迹已保存：%s", trace_path)
        return report_path, trace_path
