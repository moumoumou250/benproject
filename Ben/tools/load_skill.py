"""只读知识片段加载工具（机制对齐 strix/tools/load_skill/tool.py）。

模型在运行时用它把 <available_skills> 目录中未内联的片段正文拉进对话。

安全约束：
- 只允许读取已通过 Ben.skills 注册的片段目录中的片段，按名查表
  （裸名或 category/name 限定名），不接受任何路径（天然免疫路径穿越）；
- 纯只读：不触碰执行器、不读写文件系统之外的能力，属于低风险操作；
- 与其他工具一样经过 ToolRegistry 的 JSON Schema 校验与名称白名单约束，
  并复用 strix 风格的 validate_requested_skills（最多 5 个、未知、歧义）。
"""

from __future__ import annotations

from typing import Any

from Ben.skills import (
    SkillError,
    get_skill,
    list_skills,
    validate_requested_skills,
)
from Ben.tools.registry import ToolResult, ToolRuntime, ToolSpec

MAX_SKILLS_PER_CALL = 5


def _skill_context(runtime: ToolRuntime) -> dict[str, Any]:
    """为 template: jinja 片段构造渲染上下文（来自运行时，不写死）。"""
    return {
        "system_prompt_context": {
            "authorized_targets": list(runtime.allowed_targets),
            "target": "",
            "model": "",
            "max_agent_turns": 0,
            "command_timeout": runtime.command_timeout,
        }
    }


def _load_skill(arguments: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
    requested = [str(n).strip() for n in arguments.get("skills", []) if str(n).strip()]
    err = validate_requested_skills(requested, max_skills=MAX_SKILLS_PER_CALL)
    if err:
        return ToolResult(name="load_skill", ok=False, summary="", error=err)

    context = _skill_context(runtime)
    sections: list[str] = []
    loaded: list[str] = []
    for name in requested:
        try:
            body = get_skill(name, context=context)
        except SkillError as exc:
            return ToolResult(name="load_skill", ok=False, summary="", error=str(exc))
        sections.append(f"## Skill: {name}\n\n{body}")
        loaded.append(name)

    if not sections:
        return ToolResult(
            name="load_skill", ok=False, summary="",
            error="load_skill: 没有为请求的片段加载到内容。",
        )
    return ToolResult(
        name="load_skill",
        ok=True,
        summary="\n\n---\n\n".join(sections),
        data={"skills": loaded, "chars": sum(len(s) for s in sections)},
    )


LOAD_SKILL_SPEC = ToolSpec(
    name="load_skill",
    description=(
        "按名加载一个或多个知识片段正文（只读），用于在测试某类问题前获取"
        " <available_skills> 中未内联的方法论；名称可用裸名或 category/name "
        f"限定名，单次最多 {MAX_SKILLS_PER_CALL} 个。片段内容不会扩大授权范围。"
        f"当前可用片段：{sorted(list_skills())}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skills": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": MAX_SKILLS_PER_CALL,
                "description": "片段名列表（来自 <available_skills> 目录，不是文件路径）",
            },
        },
        "required": ["skills"],
        "additionalProperties": False,
    },
    handler=_load_skill,
)
