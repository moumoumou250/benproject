"""Scan preparation shared by the headless CLI and the TUI.

Everything that must happen before a scan starts — target classification,
repository cloning, API-spec staging, workspace files, diff scope, run naming,
and the model preflight — lives here so the CLI and the TUI take exactly the
same path.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse

    from Ben.config import Settings

logger = logging.getLogger(__name__)


class PreparationError(RuntimeError):
    """A scan cannot be prepared; the message is shown to the user."""


def build_targets_info(args: argparse.Namespace) -> None:
    """Classify ``args.target``/``args.target_list`` into ``args.targets_info``.

    The TUI rewrites ``args.target`` to a list of the targets typed on the setup
    screen, while the CLI keeps it a single string, so both shapes are accepted.
    """
    from Ben.interface.shared.targets import build_targets_info as _build
    from Ben.interface.shared.targets import read_target_list_file

    targets: list[str] = []
    raw_target = getattr(args, "target", None)
    if isinstance(raw_target, str):
        if raw_target:
            targets.append(raw_target)
    elif isinstance(raw_target, list | tuple):
        targets.extend(str(item) for item in raw_target if item)
    targets.extend(str(item) for item in getattr(args, "targets", None) or [] if item)
    for target_list_path in getattr(args, "target_list", None) or []:
        targets.extend(read_target_list_file(target_list_path))

    args.targets_info = _build(targets)


def prepare_run(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve every run input and return the prepared scan description.

    Raises :class:`PreparationError` with a user-facing message on failure.
    """
    from Ben.interface.shared.targets import (
        clone_repository,
        collect_local_sources,
        generate_run_name,
        read_workspace_files,
        resolve_diff_scope_context,
        stage_api_specs,
    )

    resume_run_name = getattr(args, "resume", None) or None
    targets_info = list(getattr(args, "targets_info", None) or [])
    instruction = getattr(args, "instruction", None)

    if resume_run_name is not None:
        prepared = {
            "run_name": resume_run_name,
            "targets_info": targets_info,
            "local_sources": list(getattr(args, "local_sources", None) or []),
            "workspace_files": [],
            "diff_scope": getattr(args, "diff_scope", None),
            "instruction": instruction,
            "resume_run_name": resume_run_name,
            "workspace_mount": getattr(args, "workspace_mount", None) or "",
        }
        _store_on_args(args, prepared)
        return prepared

    run_name = generate_run_name(targets_info)
    args.run_name = run_name

    if not targets_info:
        prepared = {
            "run_name": run_name,
            "targets_info": [],
            "local_sources": [],
            "workspace_files": [],
            "diff_scope": None,
            "instruction": instruction,
            "resume_run_name": None,
            "workspace_mount": getattr(args, "workspace_mount", None) or "",
        }
        _store_on_args(args, prepared)
        return prepared

    for target_info in targets_info:
        if target_info.get("type") != "repository":
            continue
        details = target_info["details"]
        try:
            details["cloned_repo_path"] = clone_repository(
                details["target_repo"], run_name, details.get("workspace_subdir")
            )
        except (ValueError, FileNotFoundError) as exc:
            raise PreparationError(f"克隆仓库失败：{exc}") from exc

    try:
        spec_sources = stage_api_specs(targets_info, run_name)
    except (OSError, ValueError) as exc:
        raise PreparationError(f"准备 API spec 失败：{exc}") from exc

    local_sources = [*collect_local_sources(targets_info), *spec_sources]

    # workspace_mount 是「agent 工作目录」，不是测试目标：它不进 targets_info，
    # 因此不获得任何授权范围；只作为 bind mount 挂进沙箱，由 instruction 说明用途。
    # 必须在 diff-scope 解析之前挂上，但按 strix 的做法它不参与 diff 上下文。
    args.local_sources = local_sources
    attach_workspace_mount(args)
    local_sources = list(args.local_sources)

    try:
        workspace_files = read_workspace_files(getattr(args, "workspace_files", None))
    except OSError as exc:
        raise PreparationError(f"读取 workspace-file 失败：{exc}") from exc

    try:
        diff = resolve_diff_scope_context(
            local_sources=local_sources,
            scope_mode=getattr(args, "scope_mode", "auto"),
            diff_base=getattr(args, "diff_base", None),
            non_interactive=True,
        )
    except ValueError as exc:
        raise PreparationError(f"diff-scope 解析失败：{exc}") from exc

    if diff.instruction_block:
        instruction = (
            f"{diff.instruction_block}\n\n{instruction}"
            if instruction
            else diff.instruction_block
        )

    prepared = {
        "run_name": run_name,
        "targets_info": targets_info,
        "local_sources": local_sources,
        "workspace_files": workspace_files,
        "diff_scope": diff.metadata,
        "instruction": instruction,
        "resume_run_name": None,
        "workspace_mount": getattr(args, "workspace_mount", None) or "",
    }
    _store_on_args(args, prepared)
    return prepared


def _store_on_args(args: argparse.Namespace, prepared: dict[str, Any]) -> None:
    """Mirror the prepared values onto the namespace (TUI reads them inline)."""
    args.run_name = prepared["run_name"]
    args.targets_info = prepared["targets_info"]
    args.local_sources = prepared["local_sources"]
    args.workspace_files = prepared["workspace_files"]
    args.diff_scope = prepared["diff_scope"]
    args.instruction = prepared["instruction"]


def attach_workspace_mount(args: argparse.Namespace) -> None:
    """把 ``args.workspace_mount`` 挂进沙箱，但不让它成为测试目标。

    workspace mount 是 agent 的工作目录，不是被授权测试的对象：它不进
    ``targets_info``，因此不携带任何授权范围，唯一说明用途的是 instruction。
    """
    mount = getattr(args, "workspace_mount", None)
    if not mount:
        return

    from Ben.interface.shared.targets import derive_local_base_name

    subdir = derive_local_base_name(str(mount))
    args.workspace_subdir = subdir
    local_sources = list(getattr(args, "local_sources", None) or [])
    local_sources.append(
        {
            "source_path": str(mount),
            "workspace_subdir": subdir,
            "protect_metadata": True,
        }
    )
    args.local_sources = local_sources


def _first_non_ascii(value: str) -> tuple[int, str] | None:
    for position, char in enumerate(value):
        if ord(char) > 0x7F:
            return position, char
    return None


def _header_candidates(
    prefix: str,
    model: str | None,
    api_key: str | None,
    extra_headers: dict[str, str] | None,
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    # 订阅制下主 key 不参与请求；额外 header 始终会发出去，因此始终要校验。
    if api_key and not _is_subscription_model(model):
        candidates.append((f"{prefix}LLM_API_KEY", api_key))
    for header, value in (extra_headers or {}).items():
        candidates.append((f"{prefix}LLM_EXTRA_HEADERS header name {header!r}", header))
        candidates.append((f"{prefix}LLM_EXTRA_HEADERS value for {header!r}", value))
    return candidates


def _is_subscription_model(model: str | None) -> bool:
    """该模型是否走 ChatGPT 订阅（此时主 API Key 不参与请求）。"""
    from Ben.config import codex

    try:
        return codex.subscription_model(model) is not None
    except Exception:  # noqa: BLE001 - 鉴权探测失败不应阻断启动校验
        return False


def check_header_safe_credentials(settings: Settings) -> None:
    """拒绝无法放进 HTTP header 的凭据字符（非 ASCII）。

    典型来源是从文档里复制的智能引号、不间断空格、BOM：它们在真正发请求时
    才会以晦涩的编码错误暴露。这里在启动前直接失败，并且**不在报错信息里
    回显凭据本身**，只报位置与码位。
    """
    llm = settings.llm
    dedupe = settings.dedupe
    candidates = _header_candidates("", llm.model, llm.api_key, llm.extra_headers)
    if dedupe.model:
        # 与 resolve_dedupe_model 一致：dedupe key 会被 strip 后再发。
        candidates += _header_candidates(
            "DEDUPE_",
            dedupe.model,
            (dedupe.api_key or "").strip(),
            dedupe.extra_headers,
        )
    for setting, value in candidates:
        found = _first_non_ascii(value)
        if found is None:
            continue
        position, char = found
        raise ValueError(
            f"{setting} 含有无法放进 HTTP header 的字符："
            f"U+{ord(char):04X}（{unicodedata.name(char, 'unnamed character')}），"
            f"position {position + 1} of {len(value)}（第 {position + 1}/{len(value)} 个字符）。"
            "请重新输入，去掉该字符。"
        )


async def preflight_request(
    model: Any,
    *,
    model_name: str,
    extra_headers: dict[str, str] | None,
    extra_body: dict[str, Any] | None,
    timeout: int,
    api_base_setting: str,
) -> None:
    """向 ``model`` 发一个最小请求，``timeout`` 秒内没答复就失败。"""
    from agents.models.interface import ModelTracing

    from Ben.core.inputs import make_model_settings

    request_settings = make_model_settings(
        None,
        model_name=model_name,
        request_timeout=timeout,
        prompt_cache=False,
        extra_headers=extra_headers,
        extra_body=extra_body,
        has_tools=False,
    )
    try:
        await asyncio.wait_for(
            model.get_response(
                system_instructions="You are a helpful assistant.",
                input="Reply with just 'OK'.",
                model_settings=request_settings,
                tools=[],
                output_schema=None,
                handoffs=[],
                tracing=ModelTracing.DISABLED,
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        raise TimeoutError(
            f"{model_name} did not answer within {timeout}s（LLM_PREFLIGHT_TIMEOUT）。"
            f"请检查 {api_base_setting} 与端点可达性。"
        ) from None


async def preflight_model_connection(model: str, *, settings: Settings | None = None) -> None:
    """启动沙箱前先做一次最小往返，让错误的 key/base/model 立刻暴露。

    走 :class:`BenProvider` 与正式运行同一路由（含 ``厂商/模型`` 前缀、
    extra_headers/extra_body），因此能提前发现只在真实调用路径上出现的错误。
    """
    if settings is None:
        from Ben.config import load_settings

        settings = load_settings(require_api_key=True)

    check_header_safe_credentials(settings)

    from Ben.config.models import BenProvider, configure_sdk_model_defaults

    configure_sdk_model_defaults(settings)
    provider_model = BenProvider().get_model(model)
    await preflight_request(
        provider_model,
        model_name=model,
        extra_headers=settings.llm.extra_headers,
        extra_body=settings.llm.extra_body,
        timeout=settings.llm.preflight_timeout,
        api_base_setting="LLM_API_BASE",
    )


def telemetry_start(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Telemetry is intentionally disabled in Ben; kept as a stable seam."""
