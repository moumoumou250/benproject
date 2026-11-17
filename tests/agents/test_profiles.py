"""Profile 工具面、ToolkitCache、shell allowlist 回归。"""

from __future__ import annotations

import pytest

from Ben.agents import factory
from Ben.agents.profiles import (
    check_profile_shell_allow,
    get_profile,
    get_toolkit_cache,
    normalize_profile,
    render_ephemeral_task,
    reset_toolkit_cache,
)
from tests.conftest import build_agent


@pytest.fixture(autouse=True)
def _reset_profile_state() -> object:
    saved = list(factory._EXTRA_TOOLS)
    factory._EXTRA_TOOLS.clear()
    reset_toolkit_cache()
    try:
        yield
    finally:
        factory._EXTRA_TOOLS[:] = saved
        reset_toolkit_cache()


def test_normalize_profile_default_is_none() -> None:
    assert normalize_profile(None) is None
    assert normalize_profile("") is None
    assert normalize_profile("default") is None
    assert normalize_profile("RECON") == "recon"
    with pytest.raises(ValueError, match="unknown"):
        normalize_profile("nope")


def test_recon_profile_mounts_minimal_tools() -> None:
    agent = build_agent(is_root=False, profile="recon")
    names = {t.name for t in agent.tools}
    assert "exec_command" in names
    assert "record_fact" in names
    assert "record_coverage" in names
    assert "agent_finish" in names
    assert "create_vulnerability_report" not in names
    assert "repeat_request" not in names
    assert "create_agent" not in names
    assert names <= {
        "exec_command",
        "write_stdin",
        "think",
        "record_coverage",
        "list_coverage",
        "record_fact",
        "list_facts",
        "agent_finish",
    }
    assert [t.name for t in agent.tools][-1] == "agent_finish"


def test_profile_none_matches_full_child() -> None:
    full = build_agent(is_root=False)
    defaulted = build_agent(is_root=False, profile=None)
    assert [t.name for t in full.tools] == [t.name for t in defaulted.tools]


def test_toolkit_cache_reuses_tools_for_same_profile() -> None:
    reset_toolkit_cache()
    a1 = build_agent(is_root=False, profile="recon")
    a2 = build_agent(is_root=False, profile="recon")
    assert len(get_toolkit_cache()) == 1
    # 同 profile 第二次应命中缓存：工具列表对象同一引用。
    assert a1.tools is a2.tools or [t.name for t in a1.tools] == [
        t.name for t in a2.tools
    ]


def test_shell_allow_denies_out_of_profile_scanner() -> None:
    allow = get_profile("recon").shell_allow
    assert check_profile_shell_allow("nmap -sV target", allow) is None
    denial = check_profile_shell_allow("sqlmap -u http://x", allow)
    assert denial is not None
    assert "sqlmap" in denial
    # 本地助手不受门控
    assert check_profile_shell_allow("cat /tmp/out.json", allow) is None
    assert check_profile_shell_allow("jq . out.json", allow) is None


def test_ephemeral_task_template_wraps_recon() -> None:
    text = render_ephemeral_task("recon", "scan 10.0.0.1 ports")
    assert "ephemeral_profile" in text
    assert "record_fact" in text
    assert "scan 10.0.0.1 ports" in text
    plain = render_ephemeral_task(None, "do stuff")
    assert plain == "do stuff"


def test_make_child_factory_accepts_profile() -> None:
    from tests.conftest import StubSandboxSession

    session = StubSandboxSession()
    factory_fn = factory.make_child_factory(sandbox_session=session)
    agent = factory_fn(name="r1", skills=[], profile="recon")
    names = {t.name for t in agent.tools}
    assert "record_fact" in names
    assert "create_vulnerability_report" not in names


def test_exploit_profile_drops_dep_delete_and_threat_save() -> None:
    names = {t.name for t in build_agent(is_root=False, profile="exploit").tools}
    assert "create_vulnerability_report" in names
    assert "update_vulnerability_report" in names
    assert "list_reports" in names
    assert "get_report" in names
    assert "create_dependency_report" not in names
    assert "delete_vulnerability_report" not in names
    assert "get_threat_model" in names
    assert "amend_threat_model" in names
    assert "save_threat_model" not in names


def test_validate_profile_drops_creates_keeps_delete() -> None:
    names = {t.name for t in build_agent(is_root=False, profile="validate").tools}
    assert "update_vulnerability_report" in names
    assert "delete_vulnerability_report" in names
    assert "create_vulnerability_report" not in names
    assert "create_dependency_report" not in names
    assert "save_threat_model" not in names
    assert "amend_threat_model" in names


def test_sca_profile_keeps_dep_create_drops_vuln_create() -> None:
    names = {t.name for t in build_agent(is_root=False, profile="sca").tools}
    assert "create_dependency_report" in names
    assert "update_vulnerability_report" in names
    assert "create_vulnerability_report" not in names
    assert "delete_vulnerability_report" not in names
    assert "save_threat_model" not in names


def test_default_child_drops_threat_save_keeps_full_reports() -> None:
    names = {t.name for t in build_agent(is_root=False).tools}
    assert {
        "create_vulnerability_report",
        "create_dependency_report",
        "update_vulnerability_report",
        "delete_vulnerability_report",
        "list_reports",
        "get_report",
    } <= names
    assert "save_threat_model" not in names
    assert "amend_threat_model" in names


def test_root_keeps_threat_save_drops_amend() -> None:
    names = {t.name for t in build_agent(is_root=True).tools}
    assert "get_threat_model" in names
    assert "save_threat_model" in names
    assert "amend_threat_model" not in names
    assert "create_vulnerability_report" not in names
