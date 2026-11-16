"""Wave 1：prompt/token 体积纪律（inherit / catalog / load_skill / subagent cap）。"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

import pytest
from agents.tool_context import ToolContext

from Ben.agents.prompt import resolve_inlined_skills
from Ben.skills import skill_catalog
from Ben.tools.agents_graph.tools import create_agent
from Ben.tools.load_skill import tool as load_skill_mod
from Ben.tools.load_skill.tool import MAX_SKILL_CHARS_PER_CALL, load_skill
from Ben.tools.reporting.tool import create_vulnerability_report


def test_create_agent_defaults_inherit_context_to_false() -> None:
    schema = create_agent.params_json_schema["properties"]["inherit_context"]
    assert schema.get("default") is False
    desc = (schema.get("description") or "").lower()
    assert "false" in desc
    assert "undumped" in desc or "opt in" in desc


def test_skill_catalog_descriptions_are_short() -> None:
    catalog = skill_catalog()
    assert catalog
    for entries in catalog.values():
        for entry in entries:
            assert len(entry["description"]) <= 40


def test_rendered_skill_catalog_is_category_grouped() -> None:
    """系统提示目录按类一行列名，不逐条贴 description（瘦身刀口 A）。"""
    from Ben.agents.prompt import render_system_prompt

    prompt = render_system_prompt(
        is_root=False,
        interactive=False,
        scan_mode="deep",
        report_tool=True,
        coverage_tools=True,
        system_prompt_context={"model": "test", "command_timeout": 30},
    )
    start = prompt.rfind("<available_skills>")
    end = prompt.find("</available_skills>", start)
    assert start >= 0 and end > start
    block = prompt[start:end]
    assert "- vulnerabilities:" in block
    # 旧格式「category/name：长描述」不得再出现在目录块
    assert "：BFLA testing" not in block
    assert "vulnerabilities/xss：" not in block


def test_create_vulnerability_report_description_is_slim() -> None:
    desc = create_vulnerability_report.description
    assert len(desc) < 6_000
    assert "markdown" in desc.lower()
    assert "fenced code" in desc.lower()
    assert "load_skill" in desc
    assert "reporting_discipline" in desc
    assert desc.count("blame") == 1


def test_resolve_inlined_skills_includes_reporting_for_agents() -> None:
    # root 与 build_agent(is_root=True) 对齐：不内联写报告纪律。
    root = resolve_inlined_skills(
        [], is_root=True, scan_mode="deep", report_tool=False
    )
    assert "analysis/reporting_discipline" not in root
    assert "coordination/root_agent" in root
    child = resolve_inlined_skills(
        ["xss"], is_root=False, scan_mode="deep", report_tool=True
    )
    assert "coordination/root_agent" not in child
    assert "analysis/reporting_discipline" in child


def _ctx(**extra: Any) -> ToolContext[dict[str, Any]]:
    return ToolContext(
        context={
            "allowed_targets": (),
            "command_timeout": 30.0,
            "supports_images": False,
            "coverage_tools": True,
            **extra,
        },
        tool_name="load_skill",
        tool_call_id="call-1",
        tool_arguments="{}",
    )


@pytest.mark.asyncio
async def test_load_skill_skips_inlined_skills() -> None:
    # 用 child 内联集验证 skip：reporting_discipline 只挂在写报告角色上。
    inlined = set(
        resolve_inlined_skills([], is_root=False, scan_mode="deep", report_tool=True)
    )
    assert "analysis/reporting_discipline" in inlined
    raw = await load_skill.on_invoke_tool(
        _ctx(inlined_skills=inlined),
        json.dumps({"skills": ["reporting_discipline"]}),
    )
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["skills"] == []
    assert "reporting_discipline" in payload["skipped"]
    assert payload["chars"] == 0


@pytest.mark.asyncio
async def test_load_skill_enforces_char_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    huge = "X" * (MAX_SKILL_CHARS_PER_CALL + 500)

    def _fake_get(name: str, context: Any = None) -> str:  # noqa: ANN401
        return huge

    monkeypatch.setattr(load_skill_mod, "get_skill", _fake_get)
    monkeypatch.setattr(
        load_skill_mod,
        "validate_requested_skills",
        lambda _names, max_skills=5: None,
    )
    monkeypatch.setattr(load_skill_mod, "resolve_skill_name", lambda n: f"cat/{n}")

    raw = await load_skill.on_invoke_tool(
        _ctx(inlined_skills=set()),
        json.dumps({"skills": ["one"]}),
    )
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload.get("truncated") is True
    assert payload["chars"] <= MAX_SKILL_CHARS_PER_CALL + 200
    assert "[... truncated" in payload["content"]


def test_ben_max_subagents_default_is_four(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEN_MAX_SUBAGENTS", raising=False)
    assert os.environ.get("BEN_MAX_SUBAGENTS", "4").strip() == "4"


def test_resolve_child_minutes_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from Ben.core.execution import _resolve_child_minutes

    monkeypatch.delenv("BEN_SUBAGENT_MINUTES", raising=False)
    assert _resolve_child_minutes(None) == 45.0
    assert _resolve_child_minutes(10) == 10.0
    monkeypatch.setenv("BEN_SUBAGENT_MINUTES", "")
    assert _resolve_child_minutes(None) is None
    monkeypatch.setenv("BEN_SUBAGENT_MINUTES", "30")
    assert _resolve_child_minutes(None) == 30.0


@pytest.mark.asyncio
async def test_spawn_refuses_when_default_subagent_cap_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未设置 env 时默认在飞上限 4：第 5 个在飞派生应被拒绝。"""
    from Ben.core import execution as execution_mod

    monkeypatch.delenv("BEN_MAX_SUBAGENTS", raising=False)

    class _Coord:
        parent_of = {f"c{i}": "root" for i in range(4)}
        statuses = {f"c{i}": "running" for i in range(4)}

    with pytest.raises(RuntimeError, match="BEN_MAX_SUBAGENTS=4"):
        await execution_mod.spawn_child_agent(
            coordinator=_Coord(),  # type: ignore[arg-type]
            factory=MagicMock(),
            agents_db_path=MagicMock(),
            sessions_to_close=[],
            run_config=MagicMock(),
            max_turns=5,
            interactive=False,
            parent_ctx={"agent_id": "root"},
            name="overflow",
            task="x",
            skills=[],
            parent_history=[],
        )


@pytest.mark.asyncio
async def test_spawn_allows_replace_when_finished_frees_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已终结的子 agent 不占在飞配额：4 个 completed 后仍可再派生。"""
    from Ben.core import execution as execution_mod

    monkeypatch.delenv("BEN_MAX_SUBAGENTS", raising=False)
    monkeypatch.setenv("BEN_SUBAGENT_MINUTES", "")  # 本测不关心时限

    class _Coord:
        parent_of = {f"c{i}": "root" for i in range(4)}
        statuses = {f"c{i}": "completed" for i in range(4)}

        async def register(self, *_a, **_k):  # noqa: ANN002, ANN003
            return None

    async def _fake_start(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(execution_mod, "_start_child_runner", _fake_start)

    result = await execution_mod.spawn_child_agent(
        coordinator=_Coord(),  # type: ignore[arg-type]
        factory=MagicMock(return_value=MagicMock()),
        agents_db_path=MagicMock(),
        sessions_to_close=[],
        run_config=MagicMock(),
        max_turns=5,
        interactive=False,
        parent_ctx={"agent_id": "root"},
        name="replacement",
        task="x",
        skills=[],
        parent_history=[],
    )
    assert result["success"] is True
    assert result["name"] == "replacement"
