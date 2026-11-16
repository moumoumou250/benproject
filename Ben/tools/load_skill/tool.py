"""load_skill @function_tool（对齐 strix 的 load_skill 工具模式）。

把旧的 ToolSpec 版 load_skill 转为 SDK 的 @function_tool 装饰函数。
"""

from __future__ import annotations

import json
from typing import Any

from agents import function_tool
from agents.run_context import RunContextWrapper

from Ben.skills import (
    SkillError,
    get_skill,
    resolve_skill_name,
    validate_requested_skills,
)

MAX_SKILLS_PER_CALL = 5
# 单次返回体积闸门（约 14k tokens 量级）；超出按请求顺序截断并明示。
MAX_SKILL_CHARS_PER_CALL = 24_000


def _runtime_from_ctx(ctx: RunContextWrapper[Any]) -> dict[str, Any]:
    """从 ctx.context 提取运行时依赖。"""
    return ctx.context if isinstance(ctx.context, dict) else {}


def _qualified(name: str) -> str:
    """解析为 category/name；解析失败时退回原名（由后续 get_skill 报错）。"""
    try:
        return resolve_skill_name(name)
    except SkillError:
        return name


@function_tool
def load_skill(
    ctx: RunContextWrapper[Any],
    skills: list[str],
) -> str:
    """按名加载一个或多个知识片段正文（只读）。

    用于在测试某类问题前获取 <available_skills> 中未内联的方法论。
    名称可用裸名或 category/name 限定名。已写入 system prompt 的片段会被跳过。

    Args:
        skills: 片段名列表（来自 <available_skills> 目录，不是文件路径）
    """
    rt = _runtime_from_ctx(ctx)
    allowed_targets = rt.get("allowed_targets", ())
    command_timeout = rt.get("command_timeout", 30.0)

    requested = [str(n).strip() for n in skills if str(n).strip()]
    err = validate_requested_skills(requested, max_skills=MAX_SKILLS_PER_CALL)
    if err:
        return json.dumps({"ok": False, "error": err}, ensure_ascii=False)

    inlined_raw = rt.get("inlined_skills") or ()
    inlined = {str(x) for x in inlined_raw}
    # 同一次 agent 生命周期内已成功加载过的也跳过，避免反复灌同一正文。
    already_loaded = rt.setdefault("_loaded_skills", set())
    if not isinstance(already_loaded, set):
        already_loaded = set(already_loaded)
        rt["_loaded_skills"] = already_loaded

    skipped: list[str] = []
    to_load: list[str] = []
    for name in requested:
        qualified = _qualified(name)
        if qualified in inlined or name in inlined or qualified in already_loaded:
            skipped.append(name)
            continue
        to_load.append(name)

    if not to_load:
        return json.dumps(
            {
                "ok": True,
                "content": "",
                "skills": [],
                "skipped": skipped,
                "chars": 0,
                "note": (
                    "these are already inlined in your system prompt "
                    "(or were loaded earlier); no need to load them again"
                ),
            },
            ensure_ascii=False,
        )

    # 为 jinja 片段构造渲染上下文（strix 片段引用的能力开关取自 runner 注入
    # 的 ctx.context；缺省按关闭处理，与无 runner 上下文时的渲染一致）
    context = {
        "supports_images": bool(rt.get("supports_images", False)),
        "coverage_tools": bool(rt.get("coverage_tools", False)),
        "system_prompt_context": {
            "authorized_targets": list(allowed_targets),
            "target": "",
            "model": "",
            "max_agent_turns": 0,
            "command_timeout": command_timeout,
        },
    }

    sections: list[str] = []
    loaded: list[str] = []
    truncated = False
    used_chars = 0
    for name in to_load:
        try:
            body = get_skill(name, context=context)
        except SkillError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        section = f"## Skill: {name}\n\n{body}"
        if used_chars + len(section) > MAX_SKILL_CHARS_PER_CALL and sections:
            truncated = True
            break
        if len(section) > MAX_SKILL_CHARS_PER_CALL and not sections:
            # 单个片段就超限：返回截断正文，明确告知。
            section = (
                section[:MAX_SKILL_CHARS_PER_CALL]
                + "\n\n[... truncated; the rest is available via load_skill "
                "on a narrower name or by reading the skill file directly]"
            )
            truncated = True
            sections.append(section)
            loaded.append(name)
            already_loaded.add(_qualified(name))
            used_chars = len(section)
            break
        sections.append(section)
        loaded.append(name)
        already_loaded.add(_qualified(name))
        used_chars += len(section)

    if not sections:
        return json.dumps(
            {"ok": False, "error": "load_skill: 没有为请求的片段加载到内容。"},
            ensure_ascii=False,
        )
    payload: dict[str, Any] = {
        "ok": True,
        "content": "\n\n---\n\n".join(sections),
        "skills": loaded,
        "chars": sum(len(s) for s in sections),
    }
    if skipped:
        payload["skipped"] = skipped
        payload["note"] = (
            "skipped skills are already inlined in your system prompt "
            "(or were loaded earlier)"
        )
    if truncated:
        payload["truncated"] = True
        payload["limit_chars"] = MAX_SKILL_CHARS_PER_CALL
        payload["truncation_note"] = (
            f"response capped at {MAX_SKILL_CHARS_PER_CALL} chars; "
            "load remaining skills in a later call"
        )
    return json.dumps(payload, ensure_ascii=False)
