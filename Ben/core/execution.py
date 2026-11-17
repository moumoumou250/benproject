"""Execution loop for addressable SDK-backed Ben agents."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from functools import cache
from typing import TYPE_CHECKING, Any, cast

from agents import ItemHelpers, MessageOutputItem, RunConfig, Runner
from agents.exceptions import AgentsException, MaxTurnsExceeded, UserError
from agents.sandbox.errors import ExecTransportError
import httpx
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
)

from Ben.config import codex
from Ben.core.hooks import (
    SUBAGENT_TURN_LIMITS_KEY,
    TURN_LIMIT_KEY,
    BudgetExceededError,
    BudgetPausedError,
    SubagentBudgetReservedError,
)
from Ben.core.inputs import child_initial_input
from Ben.core.tiers import VALID_TIERS as _VALID_TIERS
from Ben.core.sessions import (
    enforce_image_budget,
    open_agent_session,
    replace_session_items,
    scrub_images_from_items,
    seed_initial_input,
    strip_all_images_from_session,
)
from Ben.llm import request_log
from Ben.llm.compaction import is_context_overflow, maybe_compact
from Ben.skills import skill_turn_limit


if TYPE_CHECKING:
    from pathlib import Path

    from agents.items import TResponseInputItem
    from agents.lifecycle import RunHooks
    from agents.memory import Session, SQLiteSession
    from agents.result import RunResultBase
    from agents.run_config import CallModelData, ModelInputData

    from Ben.core.agents import AgentCoordinator, Status


logger = logging.getLogger(__name__)

StreamEventSink = Callable[[str, Any], None]

_INPUT_REJECTION_CODES = frozenset({400, 404, 422})
# Replies meaning "this model takes no images", not image errors in general: a
# context overflow that counts "image/vision expansion" must not match.
_IMAGE_REJECTION = re.compile(
    r"no endpoints found that support image input"  # OpenRouter
    r"|image_url is only supported by certain models"  # OpenAI
    r"|is not a multimodal model|at most 0 image\(s\)"  # vLLM
    r"|does not support image input"  # LiteLLM's Fireworks check
    r"|doesn't support the image field"  # Bedrock Converse
    r"|unknown variant `image_url`"  # DeepSeek, e.g. via Vercel AI Gateway
    r"|'[^']*image[^']*' functionality not supported",  # Vercel AI Gateway (AI SDK), unverified
    re.IGNORECASE,
)
_MAX_COMPACTIONS_PER_CYCLE = 2


@cache
def _teardown_sandbox_errors() -> tuple[type[BaseException], ...]:
    """Sandbox-gone errors, tolerated during shutdown.

    The Docker SDK is imported here rather than at module scope: it is only
    reachable with the Docker runtime backend, and importing it eagerly puts it
    on every launch's critical path.
    """
    from docker import errors as docker_errors  # type: ignore[import-untyped, unused-ignore]

    return (ExecTransportError, docker_errors.NotFound)


class ProviderRefusalError(AgentsException):
    """Raised when a provider returns a structured refusal instead of an exception."""


def _structured_provider_refusal(result: Any) -> str | None:
    for item in getattr(result, "new_items", ()) or ():
        raw_item = getattr(item, "raw_item", None)
        for content in getattr(raw_item, "content", ()) or ():
            if getattr(content, "type", None) != "refusal":
                continue
            refusal = getattr(content, "refusal", None)
            if isinstance(refusal, str) and refusal.strip():
                return refusal.strip()
            return "The model provider refused this request."
    return None


def _run_config_model(run_config: RunConfig) -> str | None:
    return run_config.model if isinstance(run_config.model, str) else None


def _agent_instructions(agent: Any) -> str:
    instructions = getattr(agent, "instructions", None)
    return instructions if isinstance(instructions, str) else ""


def _agent_tools_text(agent: Any) -> str:
    parts: list[str] = []
    for tool in getattr(agent, "tools", []) or []:
        name = getattr(tool, "name", "")
        description = getattr(tool, "description", "") or ""
        schema = getattr(tool, "params_json_schema", "") or ""
        parts.append(f"{name} {description} {schema}")
    return "\n".join(parts)


async def _compact_session(
    agent: Any, session: Session, run_config: RunConfig, *, force: bool
) -> bool:
    model = _run_config_model(run_config)
    if session is None or model is None:
        return False
    return await maybe_compact(
        session,
        model=model,
        instructions=_agent_instructions(agent),
        tools_text=_agent_tools_text(agent),
        force=force,
    )


_TEXT_ONLY_IMAGE_TEXT = "[error: this model cannot view images; use `snapshot -i` instead]"


def _with_image_scrub(run_config: RunConfig, context: dict[str, Any]) -> RunConfig:
    if context.get("supports_images", True):
        return run_config
    # Chain any filter already set; it sees the scrubbed input.
    inner = run_config.call_model_input_filter

    async def _scrub(data: CallModelData[Any]) -> ModelInputData:
        model_data = replace(
            data.model_data,
            input=scrub_images_from_items(data.model_data.input, text=_TEXT_ONLY_IMAGE_TEXT),
        )
        if inner is None:
            return model_data
        result = inner(replace(data, model_data=model_data))
        return await result if inspect.isawaitable(result) else result

    return replace(run_config, call_model_input_filter=_scrub)


_MAX_TRANSIENT_MODEL_RETRIES = 5
_TRANSIENT_MODEL_RETRY_BASE_DELAY_S = 2.0
_TRANSIENT_MODEL_RETRY_MAX_DELAY_S = 90.0
# 墙钟预算：一次运行中瞬态重试的累计耗时上限（含 sleep 与重放本身）。
# 职责边界：SDK 层 DEFAULT_MODEL_RETRY 在单个请求内原地重试（便宜、
# 不丢回合进度）；本层是回合级重放（贵，多 agent 并发时代价成倍放大）。
# 仅靠次数上限时，单次重放可能挂起几十分钟（慢响应/半开连接）——
# 预算保证整段重试在该时长内收手并走失败路径，与次数上限取先到者。
_TRANSIENT_MODEL_RETRY_BUDGET_S = 480.0


def _model_error_status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _is_image_rejection(exc: BaseException) -> bool:
    return _model_error_status_code(exc) in _INPUT_REJECTION_CODES and bool(
        _IMAGE_REJECTION.search(str(exc))
    )


def _is_transient_model_error(exc: BaseException) -> bool:
    if codex.is_content_guardrail_error(exc):
        return False
    if isinstance(
        exc, APITimeoutError | APIConnectionError | TimeoutError | ConnectionError | OSError
    ):
        return True
    # 裸 httpx 传输层错误：openai SDK 未包装时直接穿透（常见于流式读取中
    # 内层抛出 httpx.ReadTimeout），同样按瞬态处理而非崩溃。
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    code = _model_error_status_code(exc)
    if code is not None:
        return code >= 400 and code not in (401, 402, 403, 404)
    return isinstance(exc, APIError)


def _retry_after_seconds(exc: BaseException) -> float | None:
    """读取服务端建议的等待秒数：优先 Retry-After 响应头，
    退化到错误文案里的 "try again after N second(s)"（Kimi 429 格式）。"""
    headers = getattr(exc, "headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after")
        except AttributeError:
            raw = None
        if raw:
            try:
                return max(0.0, float(str(raw).strip()))
            except ValueError:
                pass
    match = re.search(r"try again after\s+(\d+(?:\.\d+)?)\s*second", str(exc), re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


def _transient_model_retry_delay(attempt: int, exc: BaseException | None = None) -> float:
    delay = _TRANSIENT_MODEL_RETRY_BASE_DELAY_S * float(2 ** (attempt - 1))
    if exc is not None:
        hinted = _retry_after_seconds(exc)
        if hinted is not None:
            # 尊重服务端节奏，但不低于自己的指数退避曲线（连续触发时
            # 提示值往往不变，退避曲线仍在增长）。
            delay = max(delay, hinted)
    return min(delay, _TRANSIENT_MODEL_RETRY_MAX_DELAY_S)


async def _salvage_stream_to_session(
    session: Session,
    pre_run_items: list[Any],
    stream: Any,
    agent_id: str,
) -> None:
    """Persist a crashed run's full history so a revived agent loses no context."""
    if stream is None:
        return
    try:
        replay = list(stream.to_input_list())
    except Exception:
        logger.exception("could not build salvage history for %s", agent_id)
        return
    desired = list(pre_run_items) + replay
    if len(desired) <= len(pre_run_items):
        return
    try:
        await replace_session_items(session, desired)
    except Exception:
        logger.exception("salvaging crashed run history failed for %s", agent_id)


async def _seed_and_prepare_first_input(
    session: Session | None, initial_input: Any, *, start_parked: bool
) -> Any:
    """Persist the opening input up front so it survives a first-turn crash."""
    if initial_input and session is not None and not start_parked:
        with contextlib.suppress(Exception):
            if await seed_initial_input(session, initial_input):
                return []
    return initial_input


async def run_agent_loop(
    *,
    agent: Any,
    initial_input: Any,
    run_config: RunConfig,
    context: dict[str, Any],
    max_turns: int,
    coordinator: AgentCoordinator,
    agent_id: str,
    interactive: bool,
    session: Session | None = None,
    start_parked: bool = False,
    event_sink: StreamEventSink | None = None,
    hooks: RunHooks[dict[str, Any]] | None = None,
) -> RunResultBase | None:
    agent_name = getattr(agent, "name", None)
    token = request_log.bind_call_context(
        agent_id, agent_name if isinstance(agent_name, str) else None
    )
    try:
        return await _run_agent_loop(
            agent=agent,
            initial_input=initial_input,
            run_config=run_config,
            context=context,
            max_turns=max_turns,
            coordinator=coordinator,
            agent_id=agent_id,
            interactive=interactive,
            session=session,
            start_parked=start_parked,
            event_sink=event_sink,
            hooks=hooks,
        )
    finally:
        request_log.reset_call_context(token)
        # H3：双保险清在飞计数，保留 heartbeat path（见 clear_llm_inflight）。
        with contextlib.suppress(Exception):
            from Ben.core.supervisor import clear_llm_inflight

            clear_llm_inflight()


async def _run_agent_loop(
    *,
    agent: Any,
    initial_input: Any,
    run_config: RunConfig,
    context: dict[str, Any],
    max_turns: int,
    coordinator: AgentCoordinator,
    agent_id: str,
    interactive: bool,
    session: Session | None,
    start_parked: bool,
    event_sink: StreamEventSink | None,
    hooks: RunHooks[dict[str, Any]] | None,
) -> RunResultBase | None:
    await coordinator.attach_runtime(
        agent_id,
        session=session,
        interrupt_on_message=interactive,
        resumable=interactive,
    )
    result: RunResultBase | None = None

    first_cycle_input = await _seed_and_prepare_first_input(
        session, initial_input, start_parked=start_parked
    )

    budget_stopped = coordinator.budget_stopped
    reserve_stopped = coordinator.reserve_stopped
    if budget_stopped:
        await coordinator.set_status(agent_id, "stopped")
        raise BudgetExceededError("scan budget reached")
    if reserve_stopped and context.get("parent_id") is not None:
        await coordinator.set_status(agent_id, "stopped")
        raise SubagentBudgetReservedError("scan reached the sub-agent budget reserve")

    if reserve_stopped and start_parked and interactive and context.get("parent_id") is None:
        await coordinator.send(agent_id, _reserve_notice(coordinator.root_finish_tool))

    if not (start_parked and interactive):
        with contextlib.suppress(BudgetPausedError):
            result = await _run_until_lifecycle(
                agent,
                coordinator,
                agent_id,
                initial_input=first_cycle_input,
                run_config=run_config,
                context=context,
                max_turns=max_turns,
                session=session,
                interactive=interactive,
                event_sink=event_sink,
                hooks=hooks,
            )

    if not interactive:
        return result

    while True:
        timeout = await _plain_waiting_timeout(coordinator, agent_id)
        try:
            woke = await coordinator.wait_for_message(agent_id, timeout=timeout)
        except asyncio.CancelledError:
            return result

        if coordinator.budget_stopped:
            await coordinator.set_status(agent_id, "stopped")
            raise BudgetExceededError("scan budget reached")

        if coordinator.reserve_stopped and context.get("parent_id") is not None:
            await coordinator.set_status(agent_id, "stopped")
            raise SubagentBudgetReservedError("scan reached the sub-agent budget reserve")

        if woke:
            # Real input is real progress, so the nudge budget starts over. A bare
            # auto-resume is not: it must not hand a wedged agent a fresh budget.
            await coordinator.reset_recovery(agent_id)
            await coordinator.reset_idle_resumes(agent_id)
        else:
            idle_resumes = await coordinator.record_idle_resume(agent_id)
            if idle_resumes >= _MAX_IDLE_AUTO_RESUMES:
                logger.warning(
                    "agent %s auto-resumed %d times without hearing from anyone; "
                    "leaving it parked until a real message arrives",
                    agent_id,
                    idle_resumes,
                )
                try:
                    from Ben.core.orchestration_metrics import record_idle_resume_cap

                    record_idle_resume_cap()
                except Exception:  # noqa: BLE001
                    pass
                await coordinator.park_waiting(agent_id, wait_kind="stalled")
                await _notify_parent_on_stall(coordinator, agent_id)
                continue
            logger.info("agent %s reached its waiting timeout; auto-resuming", agent_id)
            await coordinator.send(
                agent_id,
                {
                    "from": "system",
                    "type": "auto_resume",
                    "content": "Waiting timeout reached. Resuming execution.",
                },
                interrupt=False,
            )

        await coordinator.consume_pending(agent_id)
        with contextlib.suppress(BudgetPausedError):
            result = await _run_until_lifecycle(
                agent,
                coordinator,
                agent_id,
                initial_input=[],
                run_config=run_config,
                context=context,
                max_turns=max_turns,
                session=session,
                interactive=True,
                event_sink=event_sink,
                hooks=hooks,
            )


def _active_subagent_count(coordinator: AgentCoordinator) -> int:
    """在飞子 agent 数（含子/孙；root 不计；已终结的释放配额）。"""
    from Ben.core.agents import ACTIVE_STATUSES

    return sum(
        1
        for aid, parent in coordinator.parent_of.items()
        if parent is not None and coordinator.statuses.get(aid) in ACTIVE_STATUSES
    )


def _active_tier_count(coordinator: AgentCoordinator, tier: str) -> int:
    """在飞且 metadata.tier 匹配的子 agent 数。"""
    from Ben.core.agents import ACTIVE_STATUSES

    return sum(
        1
        for aid, parent in coordinator.parent_of.items()
        if parent is not None
        and coordinator.statuses.get(aid) in ACTIVE_STATUSES
        and str((coordinator.metadata.get(aid) or {}).get("tier") or "") == tier
    )


def _normalize_tier(tier: str | None) -> str:
    value = (tier or "hunter").strip().lower()
    if value not in _VALID_TIERS:
        raise RuntimeError(
            f"create_agent tier must be one of {sorted(_VALID_TIERS)}, got {tier!r}"
        )
    return value


def _resolve_child_minutes(minutes: float | None) -> float | None:
    """解析 per-task 时限（分钟）。

    ``create_agent(minutes=...)`` 优先；否则读 ``BEN_SUBAGENT_MINUTES``（默认 45）；
    设空字符串关闭 wall-clock 上限（仍受 MAX_AGENT_TURNS 约束）。
    """
    if minutes is not None:
        if minutes <= 0:
            raise RuntimeError("create_agent minutes must be > 0 when set")
        return float(minutes)
    raw = os.environ.get("BEN_SUBAGENT_MINUTES", "45").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"BEN_SUBAGENT_MINUTES must be a number, got {raw!r}"
        ) from exc
    if value <= 0:
        raise RuntimeError(f"BEN_SUBAGENT_MINUTES must be > 0, got {value}")
    return value


async def spawn_child_agent(
    *,
    coordinator: AgentCoordinator,
    factory: Any,
    agents_db_path: Path,
    sessions_to_close: list[SQLiteSession],
    run_config: RunConfig,
    max_turns: int,
    interactive: bool,
    parent_ctx: dict[str, Any],
    name: str,
    task: str,
    skills: list[str],
    parent_history: list[Any],
    minutes: float | None = None,
    surface: str | None = None,
    risk_area: str | None = None,
    tier: str | None = "hunter",
    profile: str | None = None,
    intent_id: str | None = None,
    event_sink: StreamEventSink | None = None,
    hooks: RunHooks[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    parent_id = parent_ctx.get("agent_id")
    if not isinstance(parent_id, str):
        raise TypeError("Parent agent_id missing from context")

    from Ben.agents.profiles import get_profile, normalize_profile, render_ephemeral_task

    resolved_tier = _normalize_tier(tier)
    surface_s = (surface or "").strip()
    risk_s = (risk_area or "").strip()
    profile_key = normalize_profile(profile)
    intent_s = (intent_id or "").strip() or None

    # intent_id → 从 task_graph 取 profile / surface / 任务简报（可覆盖显式参数）。
    # 认领推迟到 child_id 分配之后，避免 pending worker 占位。
    if intent_s:
        from Ben.core.task_graph import get_intent

        intent = get_intent(intent_s)
        if intent is None:
            raise RuntimeError(f"unknown intent_id {intent_s}")
        if intent.get("status") not in {"open", "claimed"}:
            raise RuntimeError(
                f"intent {intent_s} status={intent.get('status')}; expected open"
            )
        if profile_key is None and intent.get("profile"):
            profile_key = normalize_profile(str(intent.get("profile")))
        if not surface_s and intent.get("surface"):
            surface_s = str(intent.get("surface")).strip()
        if not risk_s and intent.get("risk_area"):
            risk_s = str(intent.get("risk_area")).strip()
        if not (task or "").strip():
            task = str(intent.get("description") or intent.get("title") or "")

    # 残局：剩余 <20% 时禁止新 surface；validator / 续领已有 claim 放行。
    from Ben.core.scan_clock import is_deadline_passed, is_endgame
    from Ben.core.task_claims import claims_enabled, owner_of

    if is_deadline_passed(parent_ctx):
        raise RuntimeError(
            "scan wall-clock deadline reached; refuse new agents — "
            "send_message_to_agent / finish_scan only"
        )
    if is_endgame(parent_ctx) and resolved_tier != "validator":
        renewing = False
        if surface_s and risk_s:
            holder = owner_of(surface_s, risk_s)
            renewing = holder is not None  # 续命/收窄已占面
        if not renewing:
            raise RuntimeError(
                "scan endgame (<20% wall-clock left): new surfaces are blocked. "
                "Use tier='validator' for existing findings, send_message_to_agent "
                "to refine an in-flight child, or finish_scan."
            )

    # Ben 硬闸门：BEN_MAX_SUBAGENTS=N 限制**同时在飞**的子 agent 数
    # （含子/孙；root 不计；completed/stopped/crashed/failed 释放配额）。
    # 默认 4；设空字符串关闭上限。达到上限时拒绝派生，create_agent 把错误回给模型。
    _max_subagents = os.environ.get("BEN_MAX_SUBAGENTS", "4").strip()
    if _max_subagents:
        try:
            _limit = int(_max_subagents)
        except ValueError:
            raise RuntimeError(
                f"BEN_MAX_SUBAGENTS must be an integer, got {_max_subagents!r}"
            ) from None
        _current = _active_subagent_count(coordinator)
        if _current >= _limit:
            raise RuntimeError(
                f"BEN_MAX_SUBAGENTS={_limit} in-flight limit reached; no new agent "
                f"will be created (refusing '{name}'; {_current} active sub-agent(s)). "
                "Wait for an agent to finish, stop a stuck one via stop_agent, or "
                "reuse an existing agent via send_message_to_agent."
            )

    if resolved_tier == "general":
        raw_gen = os.environ.get("BEN_MAX_GENERALS", "1").strip()
        if raw_gen:
            try:
                gen_limit = int(raw_gen)
            except ValueError:
                raise RuntimeError(
                    f"BEN_MAX_GENERALS must be an integer, got {raw_gen!r}"
                ) from None
            gen_current = _active_tier_count(coordinator, "general")
            if gen_current >= gen_limit:
                raise RuntimeError(
                    f"BEN_MAX_GENERALS={gen_limit} in-flight limit reached "
                    f"(refusing general '{name}'; {gen_current} active). "
                    "Wait for a general to finish or use tier='hunter'."
                )

    child_minutes = _resolve_child_minutes(minutes)
    child_id = uuid.uuid4().hex[:8]

    # 认领：child_id 已知后、register 前；缺 surface/risk 时跳过（提示词要求带上）。
    if surface_s and risk_s:
        from Ben.core.orchestration_metrics import record_claim_ok, record_claim_reject
        from Ben.core.task_claims import try_claim

        ttl = (child_minutes or 45.0) * 1.5
        claim_result = try_claim(
            surface_s,
            risk_s,
            agent_id=child_id,
            agent_name=name,
            ttl_minutes=ttl,
        )
        if not claim_result.get("success"):
            record_claim_reject()
            raise RuntimeError(str(claim_result.get("error") or "claim failed"))
        if claims_enabled() and not claim_result.get("bypassed"):
            record_claim_ok()

    # recall：把相似历史拼进 task（保守、本地字符串）。
    from Ben.core.recall import augment_task_with_recall

    task_for_child = render_ephemeral_task(profile_key, task)
    task_augmented = augment_task_with_recall(task_for_child)

    # 模型档位：general → RunConfig 旗舰（model_name=None）；其余 → flash 闭包默认。
    import inspect

    factory_kwargs: dict[str, Any] = {"name": name, "skills": skills}
    if resolved_tier == "general":
        factory_kwargs["model_name"] = None
        factory_kwargs["use_root_capabilities"] = True
    if profile_key is not None:
        factory_kwargs["profile"] = profile_key
    params = inspect.signature(factory).parameters
    if "model_name" not in params:
        if "model_name" in factory_kwargs or "use_root_capabilities" in factory_kwargs:
            logger.warning(
                "factory does not accept model_name; general tier degrades to flash"
            )
        factory_kwargs.pop("model_name", None)
        factory_kwargs.pop("use_root_capabilities", None)
    if "profile" not in params:
        factory_kwargs.pop("profile", None)

    # profile.max_turns 可收紧本 child 的回合上限（不抬高全局 max_turns）。
    child_max_turns = max_turns
    if profile_key is not None:
        spec = get_profile(profile_key)
        if spec.max_turns is not None:
            child_max_turns = min(max_turns, spec.max_turns)

    registered = False
    try:
        child_agent = factory(**factory_kwargs)

        await coordinator.register(
            child_id,
            name,
            parent_id,
            task=task_augmented,
            skills=skills,
        )
        registered = True
        # metadata 在 register 后补 tier/surface；兼容测试用的简易 coordinator stub。
        meta_map = getattr(coordinator, "metadata", None)
        if isinstance(meta_map, dict):
            meta = meta_map.setdefault(child_id, {})
            meta["tier"] = resolved_tier
            if surface_s:
                meta["surface"] = surface_s
            if risk_s:
                meta["risk_area"] = risk_s
            if profile_key:
                meta["profile"] = profile_key
            if intent_s:
                meta["intent_id"] = intent_s
        else:
            coordinator.metadata = {
                child_id: {
                    "tier": resolved_tier,
                    **({"surface": surface_s} if surface_s else {}),
                    **({"risk_area": risk_s} if risk_s else {}),
                    **({"profile": profile_key} if profile_key else {}),
                    **({"intent_id": intent_s} if intent_s else {}),
                }
            }

        batch_intent_ids: list[str] = []
        if intent_s:
            from Ben.agents.profiles import get_profile as _get_profile
            from Ben.core.task_graph import claim_intent, claim_sibling_batch

            claim = claim_intent(intent_s, worker=child_id)
            if not claim.get("success"):
                # 已被他人 claimed：仍允许同 worker 重入；否则失败。
                existing = claim.get("intent") or {}
                if existing.get("worker") != child_id:
                    raise RuntimeError(str(claim.get("error") or "intent claim failed"))
            # 合批：同父同 profile 兄弟一并 claim，finish/reaper 成对收口。
            spec_for_batch = _get_profile(profile_key)
            if spec_for_batch.batch_ok:
                batch_intent_ids = claim_sibling_batch(
                    intent_s, child_id, limit=5
                )
            else:
                batch_intent_ids = [intent_s]
            if isinstance(meta_map, dict):
                meta = meta_map.setdefault(child_id, {})
                meta["batch_intent_ids"] = list(batch_intent_ids)
            elif isinstance(getattr(coordinator, "metadata", None), dict):
                coordinator.metadata.setdefault(child_id, {})[
                    "batch_intent_ids"
                ] = list(batch_intent_ids)

        await _start_child_runner(
            parent_ctx=parent_ctx,
            coordinator=coordinator,
            agents_db_path=agents_db_path,
            sessions_to_close=sessions_to_close,
            run_config=run_config,
            max_turns=child_max_turns,
            interactive=interactive,
            child_agent=child_agent,
            child_id=child_id,
            name=name,
            parent_id=parent_id,
            task=task_augmented,
            skills=skills,
            initial_input=child_initial_input(
                name=name,
                child_id=child_id,
                parent_id=parent_id,
                task=task_augmented,
                parent_history=parent_history,
            ),
            minutes=child_minutes,
            event_sink=event_sink,
            hooks=hooks,
            profile=profile_key,
            intent_id=intent_s,
            batch_intent_ids=tuple(batch_intent_ids),
        )
    except BaseException:
        # H7：claim 在 register 之前；失败必须直接 release，不能依赖 set_status。
        if surface_s and risk_s:
            from Ben.core.task_claims import release_for_agent

            release_for_agent(child_id)
        if intent_s:
            with contextlib.suppress(Exception):
                from Ben.core.task_graph import release_intents_for_agent

                release_intents_for_agent(child_id)
        if registered:
            with contextlib.suppress(Exception):
                await coordinator.set_status(
                    child_id, "failed", error="spawn failed"
                )
        raise

    message = (
        f"Spawned '{name}' ({child_id}) tier={resolved_tier} running in parallel."
    )
    if profile_key:
        message += f" profile={profile_key}."
    if child_minutes is not None:
        message += f" Time box: {child_minutes:g} minute(s)."
    return {
        "success": True,
        "agent_id": child_id,
        "name": name,
        "parent_id": parent_id,
        "minutes": child_minutes,
        "tier": resolved_tier,
        "surface": surface_s or None,
        "risk_area": risk_s or None,
        "profile": profile_key,
        "intent_id": intent_s,
        "message": message,
    }


async def respawn_subagents(
    *,
    coordinator: AgentCoordinator,
    factory: Any,
    agents_db_path: Path,
    sessions_to_close: list[SQLiteSession],
    run_config: RunConfig,
    max_turns: int,
    interactive: bool,
    parent_ctx: dict[str, Any],
    root_id: str,
    event_sink: StreamEventSink | None = None,
    hooks: RunHooks[dict[str, Any]] | None = None,
) -> None:
    async with coordinator._lock:
        agents_snapshot = [
            (aid, status, dict(coordinator.metadata.get(aid, {})))
            for aid, status in coordinator.statuses.items()
        ]
        candidates: list[tuple[str, str, str | None, dict[str, Any]]] = []
        for aid, status, md in agents_snapshot:
            if not interactive and status not in {"running", "waiting"}:
                continue
            if coordinator.parent_of.get(aid) is None or aid == root_id:
                continue
            md["_restored_status"] = status
            candidates.append(
                (
                    aid,
                    coordinator.names.get(aid, aid),
                    coordinator.parent_of.get(aid),
                    md,
                )
            )

    for child_id, name, parent_id, md in candidates:
        try:
            restored_status = str(md.get("_restored_status") or "running")
            start_parked = interactive and restored_status != "running"

            if start_parked:
                logger.warning(
                    "respawn %s (%s): starting parked from status=%s",
                    child_id,
                    name,
                    restored_status,
                )

            child_skills = list(md.get("skills") or [])
            child_agent = factory(name=name, skills=child_skills)
            await _start_child_runner(
                parent_ctx=parent_ctx,
                coordinator=coordinator,
                agents_db_path=agents_db_path,
                sessions_to_close=sessions_to_close,
                run_config=run_config,
                max_turns=max_turns,
                interactive=interactive,
                child_agent=child_agent,
                child_id=child_id,
                name=name,
                parent_id=parent_id,
                task=str(md.get("task", "")),
                skills=child_skills,
                initial_input=[],
                start_parked=start_parked,
                event_sink=event_sink,
                hooks=hooks,
            )
            logger.info(
                "respawned %s (%s) parent=%s task_len=%d",
                child_id,
                name,
                parent_id or "-",
                len(md.get("task", "")),
            )
        except Exception:
            logger.exception("respawn %s failed; marking crashed", child_id)
            with contextlib.suppress(Exception):
                await coordinator.set_status(child_id, "crashed")


_INTERACTIVE_TOOL_RECOVERY_LIMIT = 3


async def _run_until_lifecycle(
    agent: Any,
    coordinator: AgentCoordinator,
    agent_id: str,
    *,
    initial_input: Any,
    run_config: RunConfig,
    context: dict[str, Any],
    max_turns: int,
    session: Session | None,
    interactive: bool,
    event_sink: StreamEventSink | None,
    hooks: RunHooks[dict[str, Any]] | None,
) -> RunResultBase | None:
    """Drive an agent until an explicit lifecycle tool settles its status.

    A turn that ends without ``finish_scan``, ``agent_finish``,
    ``wait_for_user``, or ``wait_for_agents`` leaves the agent ``running``:
    plain text never terminates a run and never yields to the user. Such a turn
    is nudged back into a tool call, bounded by a recovery limit. The same
    budget covers a ``wait_for_user`` call made before anything was said to the
    user since their last message: plain text is the only channel to them, so
    that park would hand them a silent turn, and the agent is sent back to
    write its reply instead.
    """
    result: RunResultBase | None = None
    input_data: Any = initial_input
    recovery_limit = _INTERACTIVE_TOOL_RECOVERY_LIMIT if interactive else max(1, max_turns)
    said_to_user = False

    while True:
        if coordinator.budget_stopped:
            await coordinator.set_status(agent_id, "stopped")
            raise BudgetExceededError("scan budget reached")

        if coordinator.reserve_stopped and context.get("parent_id") is not None:
            await coordinator.set_status(agent_id, "stopped")
            raise SubagentBudgetReservedError("scan reached the sub-agent budget reserve")

        try:
            if interactive:
                result = await _run_cycle_parked(
                    agent,
                    coordinator,
                    agent_id,
                    input_data=input_data,
                    run_config=run_config,
                    context=context,
                    max_turns=max_turns,
                    session=session,
                    event_sink=event_sink,
                    hooks=hooks,
                )
            else:
                result = await _run_cycle(
                    agent,
                    coordinator,
                    agent_id,
                    input_data=input_data,
                    run_config=run_config,
                    context=context,
                    max_turns=max_turns,
                    session=session,
                    interactive=False,
                    event_sink=event_sink,
                    hooks=hooks,
                )
        except BudgetPausedError as exc:
            if coordinator.budget_policy != "pause" and not exc.operator_pause:
                raise
            # The agent parked right before an LLM call; everything up to that
            # point is already in its session. Once resumed, the same call goes
            # out with nothing added to the conversation.
            await coordinator.wait_for_budget_resume(agent_id, parked_epoch=exc.resume_epoch)
            if (
                not coordinator.budget_stopped
                and await _agent_status(coordinator, agent_id) != "running"
            ):
                # Stopped while parked (operator stop or a parent's stop_agent).
                await coordinator.reset_recovery(agent_id)
                return result
            await coordinator.consume_pending(agent_id)
            if session is not None:
                input_data = []
            continue

        said_to_user = said_to_user or _said_to_user(result)
        # Atomic: only an agent still parked on the user is put back to work, so
        # a stop that lands in between is never overwritten.
        silent_yield = (
            interactive and not said_to_user and await coordinator.resume_silent_user_wait(agent_id)
        )
        status = await _agent_status(coordinator, agent_id)
        if status != "running" and not silent_yield:
            await coordinator.reset_recovery(agent_id)
            return result

        recoveries = await coordinator.record_recovery(agent_id)
        _log_recovery(
            agent_id,
            result,
            recoveries,
            recovery_limit,
            interactive=interactive,
            silent_yield=silent_yield,
        )
        if recoveries >= recovery_limit:
            return await _exhausted_recovery(coordinator, agent_id, result, interactive=interactive)

        input_data = await _append_tool_required_message(
            session=session,
            context=context,
            root_finish_tool=coordinator.root_finish_tool,
            attempt=recoveries,
            limit=recovery_limit,
            interactive=interactive,
            silent_yield=silent_yield,
        )


async def _exhausted_recovery(
    coordinator: AgentCoordinator,
    agent_id: str,
    result: RunResultBase | None,
    *,
    interactive: bool,
) -> RunResultBase | None:
    """Settle an agent that never recovered into a tool call.

    Interactive runs park instead of dying: a human is attached and can message
    any agent, so the scan stays resumable. Autonomous runs have nobody to
    resume them, so they fail loudly.
    """
    if not interactive:
        await coordinator.set_status(agent_id, "crashed")
        await notify_parent_on_terminal(coordinator, agent_id, "crashed")
        raise MaxTurnsExceeded(
            "Agent exhausted recovery attempts without calling finish_scan or agent_finish."
        )

    logger.warning(
        "agent %s exhausted tool-call recovery attempts; parking until a message arrives",
        agent_id,
    )
    await coordinator.park_waiting(agent_id, wait_kind="stalled")
    # A parked child owes its parent a completion report it can no longer send. The
    # parent is an agent, not a watching human, so nothing else tells it to stop
    # waiting and it burns its full timeout on a message that is never coming.
    await _notify_parent_on_stall(coordinator, agent_id)
    return result


_WAITING_AUTO_RESUME_TIMEOUT_S = 300.0

# An agent that parks again after every auto-resume makes no progress, so stop
# spending a model turn per timeout and leave it parked for a real message.
_MAX_IDLE_AUTO_RESUMES = 3


async def _plain_waiting_timeout(
    coordinator: AgentCoordinator,
    agent_id: str,
) -> float | None:
    """Auto-resume timeout for a parked agent; None waits until a message arrives.

    Driven by what the agent is waiting on, not by where it sits in the graph:
    the user can message any agent, so an agent awaiting a human parks
    indefinitely whether or not it is the root. Only an agent awaiting other
    agents is re-checked on a timer, and only until it has spent its idle
    budget re-parking without hearing anything.
    """
    async with coordinator._lock:
        status = coordinator.statuses.get(agent_id)
        has_error = agent_id in coordinator.errors
        runtime = coordinator.runtimes.get(agent_id)
        gated = runtime.user_wake_required if runtime is not None else False
        wait_kind = coordinator.wait_kinds.get(agent_id)
        idle_resumes = coordinator.idle_resume_counts.get(agent_id, 0)
    if status != "waiting" or has_error or gated:
        return None
    if wait_kind != "agents" or idle_resumes >= _MAX_IDLE_AUTO_RESUMES:
        return None
    return _WAITING_AUTO_RESUME_TIMEOUT_S


async def _run_cycle_parked(
    agent: Any,
    coordinator: AgentCoordinator,
    agent_id: str,
    *,
    input_data: Any,
    run_config: RunConfig,
    context: dict[str, Any],
    max_turns: int,
    session: Session | None,
    event_sink: StreamEventSink | None,
    hooks: RunHooks[dict[str, Any]] | None,
) -> RunResultBase | None:
    """Interactive run cycle that parks on any error instead of killing the runner."""
    try:
        return await _run_cycle(
            agent,
            coordinator,
            agent_id,
            input_data=input_data,
            run_config=run_config,
            context=context,
            max_turns=max_turns,
            session=session,
            interactive=True,
            event_sink=event_sink,
            hooks=hooks,
        )
    except (BudgetExceededError, BudgetPausedError, SubagentBudgetReservedError):
        raise
    except Exception as exc:
        logger.exception("error escaped the run cycle for %s; parking as failed", agent_id)
        await coordinator.set_status(agent_id, "failed", error=request_log.failure_text(exc))
        await notify_parent_on_terminal(coordinator, agent_id, "failed")
        return None


async def _run_cycle(  # noqa: PLR0912, PLR0915
    agent: Any,
    coordinator: AgentCoordinator,
    agent_id: str,
    *,
    input_data: Any,
    run_config: RunConfig,
    context: dict[str, Any],
    max_turns: int,
    session: Session | None,
    interactive: bool,
    event_sink: StreamEventSink | None,
    hooks: RunHooks[dict[str, Any]] | None,
) -> RunResultBase | None:
    image_strips = 0
    compactions = 0
    model_retries = 0
    model_retry_started: float | None = None
    request_log.set_retry_attempt(0)
    while True:
        stream: Any = None
        pre_run_items: list[Any] = []
        try:
            await coordinator.mark_running(agent_id)
            if session is not None:
                max_images = context.get("max_context_images")
                if isinstance(max_images, int):
                    try:
                        await enforce_image_budget(session, max_images)
                    except Exception:
                        logger.exception("image-budget enforcement failed for %s", agent_id)
                try:
                    await _compact_session(agent, session, run_config, force=False)
                except Exception:
                    logger.exception("proactive compaction failed for %s", agent_id)
                with contextlib.suppress(Exception):
                    pre_run_items = list(await session.get_items())
            stream = Runner.run_streamed(
                agent,
                input=input_data,
                run_config=_with_image_scrub(run_config, context),
                context=context,
                max_turns=max_turns,
                session=session,
                hooks=hooks,
            )
            await coordinator.attach_stream(agent_id, stream)
            try:
                try:
                    async for event in stream.stream_events():
                        if event_sink is not None:
                            try:
                                event_sink(agent_id, event)
                            except Exception:
                                logger.exception("stream event sink failed for %s", agent_id)
                    if stream.run_loop_exception is not None:
                        raise stream.run_loop_exception
                    if refusal := _structured_provider_refusal(stream):
                        raise ProviderRefusalError(refusal)
                except (BudgetExceededError, BudgetPausedError, SubagentBudgetReservedError):
                    raise
                except RuntimeError as stream_exc:
                    if "after shutdown" not in str(stream_exc):
                        raise
                    logger.warning(
                        "Ignoring LiteLLM end-of-stream shutdown race for %s",
                        agent_id,
                    )
                except _teardown_sandbox_errors():
                    if not coordinator.is_shutting_down:
                        raise
                    logger.warning(
                        "Ignoring sandbox container error during teardown for %s",
                        agent_id,
                        exc_info=True,
                    )
            finally:
                await coordinator.detach_stream(agent_id, stream)
        except BudgetPausedError as exc:
            logger.info("agent %s paused at the scan budget limit: %s", agent_id, exc)
            if coordinator.budget_policy == "pause" or exc.operator_pause:
                await coordinator.park_for_budget(agent_id)
            else:
                await coordinator.pause_for_budget(agent_id)
            raise
        except SubagentBudgetReservedError as exc:
            logger.info("sub-agent %s stopped at the budget reserve: %s", agent_id, exc)
            await coordinator.set_status(agent_id, "stopped")
            await _notify_root_on_budget_reserve(coordinator)
            raise
        except BudgetExceededError as exc:
            logger.info(
                "agent %s reached the scan budget limit; stopping the scan: %s", agent_id, exc
            )
            await coordinator.set_status(agent_id, "stopped")
            await coordinator.trigger_budget_stop()
            raise
        except Exception as exc:
            if image_strips < 3 and session is not None and _is_image_rejection(exc):
                try:
                    stripped = await strip_all_images_from_session(session)
                except Exception:
                    logger.exception("image-strip recovery failed for %s", agent_id)
                    stripped = False
                if stripped:
                    image_strips += 1
                    logger.info(
                        "Stripped images from %s session after rejection; retrying (%d)",
                        agent_id,
                        image_strips,
                    )
                    input_data = []
                    continue
            if (
                compactions < _MAX_COMPACTIONS_PER_CYCLE
                and session is not None
                and is_context_overflow(exc)
            ):
                try:
                    compacted = await _compact_session(agent, session, run_config, force=True)
                except Exception:
                    logger.exception("overflow compaction recovery failed for %s", agent_id)
                    compacted = False
                if compacted:
                    compactions += 1
                    logger.info(
                        "Compacted %s session after context overflow; retrying (%d)",
                        agent_id,
                        compactions,
                    )
                    input_data = []
                    continue
            if _is_transient_model_error(exc):
                if model_retry_started is None:
                    model_retry_started = time.monotonic()
                delay = _transient_model_retry_delay(model_retries + 1, exc)
                budget_spent = time.monotonic() - model_retry_started
                if (
                    model_retries < _MAX_TRANSIENT_MODEL_RETRIES
                    and budget_spent + delay <= _TRANSIENT_MODEL_RETRY_BUDGET_S
                ):
                    model_retries += 1
                    logger.warning(
                        "transient model/provider error for %s; replaying turn "
                        "(attempt %d/%d, backoff %.1fs, retry budget %.0f/%.0fs): %r",
                        agent_id,
                        model_retries,
                        _MAX_TRANSIENT_MODEL_RETRIES,
                        delay,
                        budget_spent,
                        _TRANSIENT_MODEL_RETRY_BUDGET_S,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    request_log.set_retry_attempt(model_retries)
                    if session is not None:
                        input_data = []
                    continue
                logger.error(
                    "transient model/provider error for %s but retry budget exhausted "
                    "(retries=%d/%d, spent=%.0fs of %.0fs); giving up: %r",
                    agent_id,
                    model_retries,
                    _MAX_TRANSIENT_MODEL_RETRIES,
                    budget_spent,
                    _TRANSIENT_MODEL_RETRY_BUDGET_S,
                    exc,
                )
            if session is not None:
                await _salvage_stream_to_session(session, pre_run_items, stream, agent_id)
            if isinstance(exc, ProviderRefusalError):
                logger.warning("agent %s refused by the model provider: %s", agent_id, exc)
                await coordinator.set_status(
                    agent_id, "failed", error=request_log.failure_text(exc)
                )
                await notify_parent_on_terminal(coordinator, agent_id, "failed")
                return None
            if isinstance(exc, MaxTurnsExceeded):
                status: Status = "stopped"
            elif isinstance(exc, UserError | AgentsException | APIError):
                status = "failed"
            else:
                status = "crashed"
            logger.exception("agent run failed for %s; marking %s", agent_id, status)
            # Settle the status and wake the parent before the exception unwinds a
            # non-interactive agent's task: a child that dies still owes its parent a
            # report, and the parent would otherwise wait out its timeout on a message
            # the dead child can no longer send.
            await coordinator.set_status(agent_id, status, error=request_log.failure_text(exc))
            await notify_parent_on_terminal(coordinator, agent_id, status)
            if not interactive:
                raise
            return None
        else:
            return cast("RunResultBase | None", stream)


async def _agent_status(coordinator: AgentCoordinator, agent_id: str) -> Status | None:
    async with coordinator._lock:
        return coordinator.statuses.get(agent_id)


def _log_recovery(
    agent_id: str,
    result: RunResultBase | None,
    attempt: int,
    limit: int,
    *,
    interactive: bool,
    silent_yield: bool,
) -> None:
    if silent_yield:
        logger.warning(
            "agent %s called wait_for_user without saying anything to the user; "
            "sending it back to reply (%d/%d)",
            agent_id,
            attempt,
            limit,
        )
        return
    logger.warning(
        "agent %s ended a turn without a lifecycle tool call (interactive=%s); "
        "forcing tool continuation (%d/%d): %s",
        agent_id,
        interactive,
        attempt,
        limit,
        _final_output_preview(result),
    )


def _said_to_user(result: RunResultBase | None) -> bool:
    """Whether the run produced any assistant text, the only channel to the user."""
    for item in getattr(result, "new_items", ()) or ():
        if isinstance(item, MessageOutputItem) and ItemHelpers.text_message_output(item).strip():
            return True
    return False


def _final_output_preview(result: RunResultBase | None) -> str:
    final_output = getattr(result, "final_output", None)
    if final_output is None:
        return "<none>"
    text = str(final_output).replace("\n", " ").strip()
    if not text:
        return "<empty>"
    return text[:300]


async def _append_tool_required_message(
    *,
    session: Session | None,
    context: dict[str, Any],
    attempt: int,
    limit: int,
    interactive: bool,
    silent_yield: bool = False,
    root_finish_tool: str = "finish_scan",
) -> list[dict[str, str]]:
    finish_tool = root_finish_tool if context.get("parent_id") is None else "agent_finish"
    if silent_yield:
        message = (
            "You called wait_for_user without having written anything to the user since "
            "their last message, so they would be handed a silent turn. Plain text is the "
            "only channel to the user: write your reply as plain text now, then call "
            f"wait_for_user. This is recovery attempt {attempt}/{limit}."
        )
    elif interactive:
        message = (
            "Your previous message ended a turn without a tool call. Plain text never ends "
            "execution and never hands control to the user: it is shown to the user, and the "
            "run continues. Continue immediately and call a tool. "
            "If you have nothing to do until the user replies, call wait_for_user; your "
            "text already reached them, so do not repeat it. "
            "If you are blocked waiting for another agent, call wait_for_agents. "
            f"If the whole engagement is complete, call {finish_tool}. "
            "Otherwise use the appropriate execution or planning tool. "
            f"This is recovery attempt {attempt}/{limit}."
        )
    else:
        message = (
            "Your previous response ended the autonomous run without a lifecycle tool "
            "call. That is invalid in non-interactive mode; plain text final answers are "
            "ignored. Continue immediately and call a tool. "
            f"If your work is complete, call {finish_tool}. "
            "If you are blocked waiting for another agent, call wait_for_agents. "
            "Otherwise use the appropriate execution or planning tool. "
            f"This is recovery attempt {attempt}/{limit}."
        )
    item = {"role": "user", "content": message}
    if session is None:
        return [item]

    await session.add_items([cast("TResponseInputItem", item)])
    return []


_TERMINAL_NOTICE = {
    "completed": (
        "[Agent completed] {name} ({agent_id}) finished and is no longer running, but it "
        "sent no completion report. Stop waiting on this child; ask it directly if you "
        "need its results."
    ),
    "crashed": (
        "[Agent crash] {name} ({agent_id}) terminated unexpectedly. "
        "Stop waiting on this child unless you want to message it again."
    ),
    "failed": (
        "[Agent failed] {name} ({agent_id}) stopped with an error and will not "
        "send a completion report. Stop waiting on this child unless you want to "
        "message it again."
    ),
    "stopped": (
        "[Agent stopped] {name} ({agent_id}) was stopped before finishing (turn limit "
        "or an explicit stop). It will not send a completion report, so stop waiting "
        "on this child; account for its unfinished subtask and continue."
    ),
}


_STALL_NOTICE = (
    "[Agent stalled] {name} ({agent_id}) kept ending turns without a tool call and is "
    "parked until it receives a message. It will not send a completion report on its "
    "own: either message it with a concrete next step to unblock it, or stop waiting on "
    "it and account for its unfinished subtask."
)


async def _notify_parent_on_stall(
    coordinator: AgentCoordinator,
    agent_id: str,
) -> None:
    """Tell the parent that a child parked mid-task, so it stops waiting blindly."""
    try:
        from Ben.core.orchestration_metrics import record_stall_notice

        record_stall_notice()
    except Exception:  # noqa: BLE001
        pass
    async with coordinator._lock:
        parent = coordinator.parent_of.get(agent_id)
        name = coordinator.names.get(agent_id, agent_id)
    if parent is None:
        return
    await coordinator.send(
        parent,
        {
            "from": agent_id,
            "type": "stalled",
            "priority": "high",
            "content": _STALL_NOTICE.format(name=name, agent_id=agent_id),
        },
        interrupt=False,
    )


async def notify_parent_on_terminal(
    coordinator: AgentCoordinator,
    agent_id: str,
    status: str,
) -> None:
    template = _TERMINAL_NOTICE.get(status)
    if template is None:
        return
    async with coordinator._lock:
        parent = coordinator.parent_of.get(agent_id)
        name = coordinator.names.get(agent_id, agent_id)
    if parent is None:
        return
    if not await coordinator.claim_parent_notice(agent_id):
        return
    await coordinator.send(
        parent,
        {
            "from": agent_id,
            "type": status,
            "priority": "high",
            "content": template.format(name=name, agent_id=agent_id),
        },
        interrupt=False,
    )


def _reserve_notice(finish_tool: str) -> dict[str, Any]:
    return {
        "from": "system",
        "type": "budget_reserve_stop",
        "priority": "high",
        "content": (
            "[Budget reserve] The scan has reached the sub-agent budget reserve: every "
            "sub-agent is being force-stopped as soon as its in-flight turn completes, and "
            "none will send a completion report. Their confirmed vulnerabilities are "
            "already filed as they were found. Do not wait on any sub-agents and do not "
            f"spawn new ones — wrap up now and call {finish_tool}."
        ),
    }


async def _notify_root_on_budget_reserve(coordinator: AgentCoordinator) -> None:
    root = await coordinator.claim_reserve_notification()
    if root is None:
        return
    await coordinator.send(root, _reserve_notice(coordinator.root_finish_tool))


async def _notify_parent_on_exit(
    coordinator: AgentCoordinator,
    agent_id: str,
) -> None:
    """Backstop for a child whose loop ended without telling its parent.

    Every terminal state counts, including ``completed``: a child that skips its
    completion report leaves the parent waiting on a message nobody will send.
    """
    status = await _agent_status(coordinator, agent_id)
    if status is None:
        return
    await notify_parent_on_terminal(coordinator, agent_id, status)


_TIMEBOX_NOTICE = (
    "[Agent time box] {name} ({agent_id}) hit its {minutes:g}-minute wall-clock "
    "limit and was stopped gracefully. It will not send a completion report — "
    "stop waiting on this child, harvest any findings already filed, and either "
    "spawn a fresh specialist with a tighter task or park this branch."
)


async def _enforce_child_timebox(
    coordinator: AgentCoordinator,
    child_id: str,
    *,
    name: str,
    minutes: float,
) -> None:
    """到期后 graceful stop（复用 request_stop 的 after_turn cancel），并通知父 agent。"""
    try:
        await asyncio.sleep(minutes * 60.0)
    except asyncio.CancelledError:
        return
    status = await _agent_status(coordinator, child_id)
    from Ben.core.agents import ACTIVE_STATUSES, TERMINAL_STATUSES

    if status is None or status in TERMINAL_STATUSES:
        return
    if status not in ACTIVE_STATUSES:
        return
    logger.info(
        "child %s (%s) hit %.3g-minute time box; requesting graceful stop",
        child_id,
        name,
        minutes,
    )
    # 子树一并收掉，避免孙 agent 在父已止损后继续烧额度。
    await coordinator.cancel_descendants_graceful(child_id)
    async with coordinator._lock:
        parent = coordinator.parent_of.get(child_id)
    if parent is None:
        return
    if not await coordinator.claim_parent_notice(child_id):
        return
    await coordinator.send(
        parent,
        {
            "from": child_id,
            "type": "stopped",
            "priority": "high",
            "content": _TIMEBOX_NOTICE.format(
                name=name, agent_id=child_id, minutes=minutes
            ),
        },
        interrupt=False,
    )


async def _start_child_runner(
    *,
    parent_ctx: dict[str, Any],
    coordinator: AgentCoordinator,
    agents_db_path: Path,
    sessions_to_close: list[SQLiteSession],
    run_config: RunConfig,
    max_turns: int,
    interactive: bool,
    child_agent: Any,
    child_id: str,
    name: str,
    parent_id: str | None,
    task: str,
    skills: list[str],
    initial_input: Any,
    start_parked: bool = False,
    minutes: float | None = None,
    event_sink: StreamEventSink | None = None,
    hooks: RunHooks[dict[str, Any]] | None = None,
    profile: str | None = None,
    intent_id: str | None = None,
    batch_intent_ids: tuple[str, ...] = (),
) -> None:
    session = open_agent_session(child_id, agents_db_path)
    sessions_to_close.append(session)
    await coordinator.attach_runtime(child_id, session=session, resumable=interactive)

    child_ctx: dict[str, Any] = dict(parent_ctx)
    child_ctx["agent_id"] = child_id
    child_ctx["parent_id"] = parent_id
    child_ctx["task"] = task
    if profile:
        child_ctx["agent_profile"] = profile
    if intent_id:
        child_ctx["intent_id"] = intent_id
    if batch_intent_ids:
        child_ctx["batch_intent_ids"] = list(batch_intent_ids)
    child_ctx[TURN_LIMIT_KEY] = skill_turn_limit(
        skills, parent_ctx.get(SUBAGENT_TURN_LIMITS_KEY) or {}
    )
    # 子 agent 的 system prompt 内联集与父不同；按 child 参数重算，供 load_skill 去重。
    from Ben.agents.prompt import resolve_inlined_skills
    from Ben.agents.profiles import get_profile

    spec = get_profile(profile)
    report_tool = spec.tool_names is None
    child_ctx["inlined_skills"] = set(
        resolve_inlined_skills(
            skills,
            scan_mode=str(parent_ctx.get("scan_mode") or "blackbox"),
            is_root=False,
            is_whitebox=bool(parent_ctx.get("is_whitebox", False)),
            is_diff_scoped=bool(parent_ctx.get("is_diff_scoped", False)),
            report_tool=report_tool,
            coverage_tools=bool(parent_ctx.get("coverage_tools", True)),
        )
    )
    child_ctx["_loaded_skills"] = set()

    async def _child_loop() -> None:
        # A budget stop is a clean scan-wide shutdown, not a child failure: the
        # child's status and parent notification are already settled in
        # ``_run_cycle``. Swallow it here so the detached task does not surface a
        # spurious "Task exception was never retrieved" warning. The root agent
        # hits the same limit on its next call and tears the scan down.
        timer: asyncio.Task[None] | None = None
        if minutes is not None:
            timer = asyncio.create_task(
                _enforce_child_timebox(
                    coordinator, child_id, name=name, minutes=minutes
                ),
                name=f"timebox-{name}-{child_id}",
            )
        try:
            await run_agent_loop(
                agent=child_agent,
                initial_input=initial_input,
                run_config=run_config,
                context=child_ctx,
                max_turns=max_turns,
                coordinator=coordinator,
                agent_id=child_id,
                interactive=interactive,
                session=session,
                start_parked=start_parked,
                event_sink=event_sink,
                hooks=hooks,
            )
        except BudgetExceededError:
            logger.info("child %s stopped after reaching the scan budget limit", child_id)
        except SubagentBudgetReservedError:
            logger.info("child %s stopped at the sub-agent budget reserve", child_id)
        except asyncio.CancelledError:
            # H2：task.cancel 必须落终态，否则快照留 running。
            status = await _agent_status(coordinator, child_id)
            from Ben.core.agents import TERMINAL_STATUSES

            if status not in TERMINAL_STATUSES:
                with contextlib.suppress(Exception):
                    await coordinator.set_status(
                        child_id, "stopped", error="cancelled"
                    )
            raise
        finally:
            if timer is not None and not timer.done():
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer
            if not coordinator.is_shutting_down:
                await _notify_parent_on_exit(coordinator, child_id)
            # ephemeral：finish 后立即关 session（图节点保留作审计）。
            # interactive resume 仍可能读 agents.db 行，但连接可关；teardown
            # 对已 close 的 session 再 close 是幂等的。
            if not interactive:
                with contextlib.suppress(Exception):
                    session.close()
                with contextlib.suppress(ValueError, AttributeError):
                    sessions_to_close.remove(session)

    task_handle = asyncio.create_task(_child_loop(), name=f"agent-{name}-{child_id}")
    await coordinator.attach_runtime(child_id, task=task_handle)
