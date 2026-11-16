"""Root prompt enforces Strix-style orchestration."""
import pytest

from Ben.agents.prompt import render_system_prompt


def test_root_must_delegate_target_work():
    prompt = render_system_prompt(is_root=True)
    assert "职责是编排，而不是亲自测试" in prompt
    assert "必须通过 create_agent" in prompt
    assert "不要亲自运行扫描器" in prompt
    assert "不调用 create_agent" not in prompt
    assert "你的第一个动作必须是调用 exec_command" not in prompt
    assert "Any work that touches the target is delegated to a subagent." in prompt
    assert "## Establish the Threat Model" in prompt
    assert "Per-turn discipline (token budget)" in prompt
    assert "at most two decisions" in prompt
    assert "Escalation ladder (wall-clock, per stuck child)" in prompt
    assert "park the branch" in prompt
    assert "BEN_SUBAGENT_MINUTES" in prompt


def test_child_still_reports_to_parent():
    prompt = render_system_prompt(is_root=False)
    assert "只完成主 Agent 委派的具体子任务" in prompt
    assert "不调用 create_agent" not in prompt
    assert "你的第一个动作必须是调用 exec_command" in prompt
    assert "<root_agent_directive>" not in prompt
    assert "## Establish the Threat Model" not in prompt


@pytest.mark.parametrize("coverage_tools", [True, False])
def test_root_skill_renders_coverage_reconciliation_conditionally(coverage_tools):
    prompt = render_system_prompt(is_root=True, coverage_tools=coverage_tools)
    assert ("## Reconcile Coverage Before Finishing" in prompt) is coverage_tools
    assert "{% if coverage_tools %}" not in prompt
