"""Root can honor explicit single-agent runtime checks."""
from Ben.agents.prompt import render_system_prompt


def test_root_supports_single_agent_verification():
    prompt = render_system_prompt(is_root=True)
    assert "不调用 create_agent" in prompt
    assert "不用打印范围声明代替网络取证" in prompt
    assert "需要接触目标的动作都由子 Agent 执行" not in prompt


def test_child_still_reports_to_parent():
    prompt = render_system_prompt(is_root=False)
    assert "只完成主 Agent 委派的具体子任务" in prompt
    assert "不调用 create_agent" not in prompt
