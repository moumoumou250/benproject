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
    validate_requested_skills,
)

MAX_SKILLS_PER_CALL = 5


def _runtime_from_ctx(ctx: RunContextWrapper[Any]) -> dict[str, Any]:
    """从 ctx.context 提取运行时依赖。"""
    inner = ctx.context if isinstance(ctx.context, dict) else {}
    return inner


@function_tool
def load_skill(
    ctx: RunContextWrapper[Any],
    skills: list[str],
) -> str:
    """按名加载一个或多个知识片段正文（只读）。

    用于在测试某类问题前获取 <available_skills> 中未内联的方法论。
    名称可用裸名或 category/name 限定名。

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
    for name in requested:
        try:
            body = get_skill(name, context=context)
        except SkillError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        sections.append(f"## Skill: {name}\n\n{body}")
        loaded.append(name)

    if not sections:
        return json.dumps(
            {"ok": False, "error": "load_skill: 没有为请求的片段加载到内容。"},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "content": "\n\n---\n\n".join(sections),
            "skills": loaded,
            "chars": sum(len(s) for s in sections),
        },
        ensure_ascii=False,
    )
