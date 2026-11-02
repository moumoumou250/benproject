"""Jinja2 模板化系统提示词渲染器，机制等价 strix/agents/prompt.py。

最终发给模型的仍是一个 system prompt 字符串，但它由“主模板骨架 +
可复用片段（skill）+ scope 子模板”在运行时渲染拼装而成；可变数据全部
通过渲染上下文注入，不在模板里写死。

对外契约保持不变：:func:`load_system_prompt` 返回一个字符串，runner 的
调用方式不变（仍传 prompts_dir；新增参数均为可选关键字参数）。

模板/片段搜索路径：
  prompts 目录（主模板、scope 子模板） + 全部已注册片段目录（Ben/skills）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateNotFound,
    UndefinedError,
)

from Ben.skills import (
    SkillNotFoundError,
    get_skill,
    register_skill_dir,
    skill_catalog,
    skill_search_dirs,
)

logger = logging.getLogger(__name__)

# 主模板 / scope 子模板的默认目录（与包同级的 prompts/）。
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MAIN_TEMPLATE = "system_prompt.jinja"
SCOPE_TEMPLATE = "scope.jinja"

# 前缀缓存分界标记：渲染结果中该标记之前为“不变前缀”，之后为“可变后缀”；
# 发送给模型前由 strip_cache_point 移除（见 load_system_prompt）。
CACHE_POINT = "<cache_point>"
_CACHE_POINT_LINE_RE = re.compile(r"^[ \t]*<cache_point>[ \t]*\r?\n?", re.MULTILINE)


class PromptRenderError(RuntimeError):
    """提示词渲染失败（明确报错，不静默返回空、不退回编造内容）。"""


# ---------------------------------------------------------------------------
# 片段选择（等价 strix 的 _resolve_skills）
# ---------------------------------------------------------------------------
# 按“用户指定 → 扫描模式（scan_modes/<mode>，diff 时叠加 scan_modes/diff）
# → 工具/分析类必需片段 → root/白盒专属”的固定顺序合并、去重（对齐 strix）。
# 同类 agent 参数相同 -> 结果序列相同 -> 共享相同前缀。
_REPORT_SKILLS: tuple[str, ...] = ("analysis/reporting_discipline",)
_COVERAGE_SKILLS: tuple[str, ...] = ("analysis/coverage_notes",)
_ROOT_SKILLS: tuple[str, ...] = ("coordination/root_agent",)
_WHITEBOX_SKILLS: tuple[str, ...] = ("scan_modes/whitebox",)


def _resolve_skills(
    requested_skills: Sequence[str] | None = None,
    *,
    scan_mode: str = "blackbox",
    is_root: bool = True,
    is_whitebox: bool = False,
    is_diff_scoped: bool = False,
    report_tool: bool = False,
    coverage_tools: bool = False,
) -> list[str]:
    """按固定顺序合并片段清单（category/name 限定名）并去重。

    未注册/歧义片段告警后跳过（对齐 strix load_skills 的 warn-and-skip）。
    """
    from Ben.skills import SkillError, resolve_skill_name

    ordered: list[str] = []
    # 1. 用户指定
    ordered.extend(requested_skills or [])
    # 2. 扫描模式（diff 范围叠加在模式之上而非替代）
    ordered.append(f"scan_modes/{scan_mode}")
    if is_diff_scoped:
        ordered.append("scan_modes/diff")
    # 3. 工具/分析类必需片段
    if report_tool:
        ordered.extend(_REPORT_SKILLS)
    if coverage_tools:
        ordered.extend(_COVERAGE_SKILLS)
    # 4. root / 白盒专属
    if is_root:
        ordered.extend(_ROOT_SKILLS)
    if is_whitebox or scan_mode == "whitebox":
        ordered.extend(_WHITEBOX_SKILLS)

    resolved: list[str] = []
    for name in ordered:
        key = (name or "").strip()
        if not key:
            continue
        try:
            qualified = resolve_skill_name(key)
        except SkillError as exc:
            logger.warning("片段解析失败，已跳过：%s", exc)
            continue
        if qualified not in resolved:
            resolved.append(qualified)
    return resolved


# ---------------------------------------------------------------------------
# 渲染上下文
# ---------------------------------------------------------------------------
def build_system_prompt_context(
    settings: Any | None = None,
    target: str | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """组装 system_prompt_context（可变数据唯一入口）。

    变量与 Settings 字段的对应关系：
      authorized_targets <- Settings.allowed_targets（授权白名单）
      target             <- runner.run(target) 的运行时目标
      model              <- Settings.deepseek_model
      max_agent_turns    <- Settings.max_agent_turns
      command_timeout    <- Settings.command_timeout
    未提供 settings 时各项取空值，模板用 {% if %} 守卫跳过对应行。
    """
    context: dict[str, Any] = {
        "authorized_targets": [],
        "target": "",
        "model": "",
        "max_agent_turns": 0,
        "command_timeout": 0,
    }
    if settings is not None:
        context.update(
            authorized_targets=list(getattr(settings, "allowed_targets", ()) or ()),
            model=getattr(settings, "deepseek_model", "") or "",
            max_agent_turns=int(getattr(settings, "max_agent_turns", 0) or 0),
            command_timeout=getattr(settings, "command_timeout", 0) or 0,
        )
    if target:
        context["target"] = target
    if overrides:
        context.update(overrides)
    return context


# ---------------------------------------------------------------------------
# Jinja 环境
# ---------------------------------------------------------------------------
def _build_environment(prompts_dir: Path | None, context: Mapping[str, Any]) -> Environment:
    """构建渲染环境：FileSystemLoader 同时挂载 prompts 目录与片段目录。

    autoescape 关闭（提示词非 HTML）；trim_blocks/lstrip_blocks 控制空行；
    StrictUndefined：未定义变量立即报错（渲染失败明确报错，不静默吞掉）。
    """
    search_path: list[str] = []
    primary = Path(prompts_dir).resolve() if prompts_dir else DEFAULT_PROMPTS_DIR
    search_path.append(str(primary))
    if primary != DEFAULT_PROMPTS_DIR and DEFAULT_PROMPTS_DIR.is_dir():
        # 自定义 prompts 目录优先，包内默认目录兜底（scope 子模板仍可解析）。
        search_path.append(str(DEFAULT_PROMPTS_DIR))
    search_path.extend(str(d) for d in skill_search_dirs())

    env = Environment(
        loader=FileSystemLoader(search_path),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        keep_trailing_newline=True,
    )
    # 模板全局函数：按名取片段正文；jinja 片段共享当前渲染上下文。
    env.globals["get_skill"] = lambda name: get_skill(name, context=context)
    return env


def _render(env: Environment, template_name: str, context: Mapping[str, Any]) -> str:
    try:
        template = env.get_template(template_name)
    except TemplateNotFound as exc:
        raise FileNotFoundError(
            f"提示词模板不存在：{template_name}（搜索路径：{env.loader.searchpath}）"
        ) from exc
    try:
        return template.render(dict(context))
    except (UndefinedError, SkillNotFoundError) as exc:
        raise PromptRenderError(
            f"模板 {template_name} 渲染失败：{type(exc).__name__}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# 主渲染入口（等价 strix 的 render_system_prompt）
# ---------------------------------------------------------------------------
def render_system_prompt(
    *,
    requested_skills: Sequence[str] | None = None,
    scan_mode: str = "blackbox",
    is_root: bool = True,
    is_whitebox: bool = False,
    is_diff_scoped: bool = False,
    interactive: bool = False,
    parallel_tool_calls: bool = False,
    include_scope: bool = True,
    supports_images: bool = False,
    report_tool: bool = False,
    coverage_tools: bool = False,
    system_prompt_context: Mapping[str, Any] | None = None,
    mcp_available: bool = False,
    mcp_connections: Sequence[Mapping[str, Any]] | None = None,
    prompts_dir: Path | None = None,
    extra_skill_dirs: Sequence[str | Path] | None = None,
) -> str:
    """渲染主模板，返回含 <cache_point> 标记的完整 system prompt 字符串。

    默认值适配 Ben 当前形态（单 root、黑盒、自主、串行、无覆盖率/上报工具）；
    当前用不到的分支保留参数与条件结构，供后续多角色扩展直接启用。
    """
    for dir_path in extra_skill_dirs or ():
        register_skill_dir(dir_path)

    # 合并默认上下文，保证模板 {% if %} 守卫在缺省调用下也可判定；
    # 未定义变量仍由 StrictUndefined 明确报错。MCP 连接信息放入
    # system_prompt_context（对齐 strix 的 scope.jinja 取数方式）。
    spc = build_system_prompt_context()
    spc.update(system_prompt_context or {})
    # runner 可经 system_prompt_context 注入 mcp_available/mcp_connections；
    # 显式参数为 False/None 时保留注入值而非无条件覆盖（对齐 strix 的
    # runner -> scope_context 注入路径）。
    spc["mcp_available"] = mcp_available or bool(spc.get("mcp_available"))
    spc["mcp_connections"] = list(mcp_connections or spc.get("mcp_connections") or ())

    context: dict[str, Any] = {
        "scan_mode": scan_mode,
        "is_root": is_root,
        "is_whitebox": is_whitebox,
        "is_diff_scoped": is_diff_scoped,
        "interactive": interactive,
        "parallel_tool_calls": parallel_tool_calls,
        "include_scope": include_scope,
        "supports_images": supports_images,
        "report_tool": report_tool,
        "coverage_tools": coverage_tools,
        "system_prompt_context": spc,
        "skill_catalog": skill_catalog(),
    }

    # 片段解析：用户请求片段在 <cache_point> 之后内联（随任务变化）；
    # 其余解析结果作为共享片段在前缀内联（同类 agent 前缀一致）。
    # 列表内为 category/name 限定名（避免裸名歧义）；模板 XML 标签取裸名
    # （对齐 strix 的 <{{ skill_name }}> 包裹样式）。
    resolved = _resolve_skills(
        requested_skills,
        scan_mode=scan_mode,
        is_root=is_root,
        is_whitebox=is_whitebox,
        is_diff_scoped=is_diff_scoped,
        report_tool=report_tool,
        coverage_tools=coverage_tools,
    )
    from Ben.skills import SkillError, resolve_skill_name

    requested_keys: set[str] = set()
    for raw in requested_skills or ():
        try:
            requested_keys.add(resolve_skill_name(raw))
        except SkillError:  # 未知名已在 _resolve_skills 告警
            continue
    context["requested"] = [q for q in resolved if q in requested_keys]
    context["shared"] = [q for q in resolved if q not in requested_keys]

    env = _build_environment(prompts_dir, context)
    return _render(env, MAIN_TEMPLATE, context)


def render_scope_prompt(
    system_prompt_context: Mapping[str, Any] | None = None,
    *,
    prompts_dir: Path | None = None,
) -> str:
    """单独渲染 scope 子模板（等价 strix 的 render_scope_prompt）。

    MCP 连接信息经 system_prompt_context.mcp_available / .mcp_connections
    传入（与 strix 一致）；无连接时对应块不输出。
    """
    spc = build_system_prompt_context()
    spc.update(system_prompt_context or {})
    spc.setdefault("mcp_available", False)
    spc.setdefault("mcp_connections", [])
    env = _build_environment(prompts_dir, {"system_prompt_context": spc})
    return _render(env, SCOPE_TEMPLATE, {"system_prompt_context": spc})


def split_cache_point(prompt: str) -> tuple[str, str]:
    """按 <cache_point> 拆分“不变前缀 / 可变后缀”（供前缀缓存使用）。"""
    prefix, sep, suffix = prompt.partition(CACHE_POINT)
    if not sep:
        return prompt, ""
    return prefix, suffix.lstrip("\r\n")


def strip_cache_point(prompt: str) -> str:
    """发送给模型前移除 <cache_point> 标记行。"""
    return _CACHE_POINT_LINE_RE.sub("", prompt)


# ---------------------------------------------------------------------------
# 对外加载函数（runner 契约保持不变：传入 prompts_dir，返回一个字符串）
# ---------------------------------------------------------------------------
def load_system_prompt(
    prompts_dir: Path | None = None,
    *,
    settings: Any | None = None,
    target: str | None = None,
    **render_options: Any,
) -> str:
    """渲染并返回发送就绪的系统提示词（已移除 <cache_point> 标记）。

    - ``prompts_dir``：模板搜索目录（默认包内 prompts/），与旧契约一致；
    - ``settings``/``target``：注入 system_prompt_context 的可变数据；
    - 其余关键字参数原样透传给 :func:`render_system_prompt`。
    """
    context = build_system_prompt_context(settings, target)
    prompt = render_system_prompt(
        system_prompt_context=context,
        prompts_dir=prompts_dir,
        **render_options,
    )
    prompt = strip_cache_point(prompt)
    logger.info("已渲染系统提示词（%d 字符）", len(prompt))
    return prompt
