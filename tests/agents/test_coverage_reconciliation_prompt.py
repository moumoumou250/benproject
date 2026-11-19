"""根 agent 结束扫描前必须对账未关闭的覆盖行。

Ben 把这条纪律放在 ``finish_scan`` 的工具描述里（而不是系统提示词）：
模型在准备调用结束工具时一定会读到它，比埋在长提示词里更有效。
"""

from __future__ import annotations

import pytest

from Ben.agents.factory import finish_scan
from tests.conftest import build_agent


_RECONCILIATION = "**Coverage reconciliation.**"
_LIST_COVERAGE = "``list_coverage``"
_PENDING = "``pending``"
_FOLLOW_UP = "``needs_follow_up``"
_SILENT_OMISSION = "never a silent omission"


def test_finish_scan_requires_coverage_reconciliation() -> None:
    description = finish_scan.description

    assert _RECONCILIATION in description
    assert _LIST_COVERAGE in description
    assert _PENDING in description
    assert _FOLLOW_UP in description
    assert _SILENT_OMISSION in description


@pytest.mark.parametrize(("scan_mode", "is_whitebox"), [("deep", False), ("quick", True)])
def test_reconciliation_is_independent_of_scan_mode_and_whitebox(
    scan_mode: str, is_whitebox: bool
) -> None:
    """结束工具的契约恒定，不随扫描模式/白盒开关漂移。"""
    agent = build_agent(
        is_root=True,
        scan_mode=scan_mode,
        is_whitebox=is_whitebox,
    )
    by_name = {tool.name: tool for tool in agent.tools}

    assert _RECONCILIATION in by_name["finish_scan"].description


def test_reconciliation_disappears_when_coverage_tools_are_off() -> None:
    agent = build_agent(is_root=True, coverage_tools=False)
    by_name = {tool.name: tool for tool in agent.tools}

    assert _RECONCILIATION not in by_name["finish_scan"].description


def test_child_agents_get_agent_finish_instead() -> None:
    child = build_agent(is_root=False)
    names = [tool.name for tool in child.tools]

    assert "agent_finish" in names
    assert "finish_scan" not in names
