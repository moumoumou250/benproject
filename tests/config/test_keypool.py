"""Key 池（Ben.config.keypool）单元测试：CRUD、env 注入语义、掩码边界。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from Ben.config import keypool
from Ben.config.keypool import (
    add_entry,
    apply_to_env,
    clear_tier,
    delete_entry,
    entry_view,
    key_tail,
    read_pool,
    switch_entry,
)
from Ben.config.keypool import test_entry as run_entry_test  # 避免被 pytest 误收集


@pytest.fixture()
def pool_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "keypool.json"
    monkeypatch.setattr(keypool, "pool_path", lambda: path)
    return path


def _add_root(model: str = "deepseek/deepseek-chat", key: str = "sk-test-12345678") -> dict[str, Any]:
    return add_entry("root", model, api_key=key)


# ---------------------------------------------------------------------------
# CRUD 与 active 语义
# ---------------------------------------------------------------------------


def test_add_entry_becomes_active(pool_file: Path) -> None:
    entry = _add_root()
    pool = read_pool()
    assert pool["active"]["root"] == entry["id"]
    assert pool["entries"][0]["model"] == "deepseek/deepseek-chat"
    # 明文 key 落盘（0600 文件内），条目本身必须有完整 key 供运行时使用
    assert pool["entries"][0]["api_key"] == "sk-test-12345678"


def test_add_invalid_tier_and_empty_model(pool_file: Path) -> None:
    with pytest.raises(ValueError, match="tier"):
        add_entry("child", "m")
    with pytest.raises(ValueError, match="模型名"):
        add_entry("root", "   ")
    with pytest.raises(ValueError, match="端点"):
        add_entry("root", "m", base_url="ftp://x")


def test_switch_entry_replaces_tier_active(pool_file: Path) -> None:
    first = _add_root()
    second = add_entry("root", "moonshot/kimi-k2.6", api_key="sk-m-99998888")
    pool = read_pool()
    assert pool["active"]["root"] == second["id"]
    switch_entry(first["id"])
    assert read_pool()["active"]["root"] == first["id"]


def test_clear_tier_falls_back_to_env(pool_file: Path) -> None:
    entry = _add_root()
    clear_tier("root")
    pool = read_pool()
    assert "root" not in pool["active"]
    assert len(pool["entries"]) == 1  # clear 只取消生效，不删条目
    assert entry["id"]


def test_delete_entry_removes_active_too(pool_file: Path) -> None:
    entry = _add_root()
    delete_entry(entry["id"])
    pool = read_pool()
    assert pool["entries"] == []
    assert "root" not in pool["active"]


def test_tiers_are_independent(pool_file: Path) -> None:
    root = add_entry("root", "deepseek/deepseek-chat", api_key="sk-r-11112222")
    flash = add_entry("flash", "glm-5.3-flash", api_key="sk-f-33334444")
    pool = read_pool()
    assert pool["active"]["root"] == root["id"]
    assert pool["active"]["flash"] == flash["id"]
    # 删除 root 不影响 flash 的 active
    delete_entry(root["id"])
    assert "root" not in read_pool()["active"]
    assert read_pool()["active"]["flash"] == flash["id"]


# ---------------------------------------------------------------------------
# apply_to_env 注入语义
# ---------------------------------------------------------------------------


def test_apply_to_env_without_entries_is_noop(pool_file: Path) -> None:
    env = {"LLM_MODEL": "keep/me", "BEN_FLASH_MODEL": "keep/flash"}
    result = dict(apply_to_env(env))
    assert result == env


def test_apply_to_env_root_entry_replaces_whole_connection(pool_file: Path) -> None:
    add_entry("root", "deepseek/deepseek-chat", api_key="sk-new-88889999", base_url="https://api.deepseek.example/v1")
    env = {
        "LLM_MODEL": "old/model",
        "LLM_API_KEY": "sk-old",
        "LLM_API_BASE": "https://old.example/v1",
        "DEEPSEEK_API_KEY": "sk-stale-from-shell",
    }
    result = dict(apply_to_env(env))
    assert result["LLM_MODEL"] == "deepseek/deepseek-chat"
    assert result["LLM_API_KEY"] == "sk-new-88889999"
    assert result["LLM_API_BASE"] == "https://api.deepseek.example/v1"
    # 厂商专属变量被覆盖为 entry 的 key（shell 残留的旧值不能用 setdefault 兜住）
    assert result["DEEPSEEK_API_KEY"] == "sk-new-88889999"


def test_apply_to_env_empty_base_explicitly_clears(pool_file: Path) -> None:
    """entry 不带端点 = 用厂商默认：写空串显式清除，不许回落 .env 旧值。"""
    add_entry("root", "deepseek/deepseek-chat", api_key="sk-a-12345678")
    env = {"LLM_API_BASE": "https://old.example/v1", "OPENAI_BASE_URL": "https://old.example/v1"}
    result = dict(apply_to_env(env))
    assert result["LLM_API_BASE"] == ""
    assert "OPENAI_BASE_URL" not in result


def test_apply_to_env_flash_tier_independent(pool_file: Path) -> None:
    add_entry("flash", "glm-5.3-flash", api_key="sk-f-77778888")
    env = {"LLM_MODEL": "grok/grok-4.6", "LLM_API_KEY": "sk-root-kept"}
    result = dict(apply_to_env(env))
    assert result["BEN_FLASH_MODEL"] == "glm-5.3-flash"
    assert result["BEN_FLASH_API_KEY"] == "sk-f-77778888"
    assert result["BEN_FLASH_API_BASE"] == ""
    # root 无 entry：原样保留
    assert result["LLM_MODEL"] == "grok/grok-4.6"
    assert result["LLM_API_KEY"] == "sk-root-kept"


def test_apply_to_env_os_environ_in_place(pool_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add_entry("root", "deepseek/deepseek-chat", api_key="sk-env-12345678")
    monkeypatch.setenv("LLM_MODEL", "before")
    apply_to_env()  # env=None -> 直接改 os.environ
    assert os.environ["LLM_MODEL"] == "deepseek/deepseek-chat"


# ---------------------------------------------------------------------------
# 掩码与状态视图：明文 key 不出边界
# ---------------------------------------------------------------------------


def test_key_tail_masks_short_and_long() -> None:
    assert key_tail("sk-abcdef123456") == "…3456"
    assert key_tail("short") == "…••••"
    assert key_tail("") is None
    assert key_tail(None) is None


def test_entry_view_never_contains_full_key(pool_file: Path) -> None:
    entry = add_entry("root", "deepseek/deepseek-chat", api_key="sk-secret-abcdef99")
    view = entry_view(entry)
    assert view["key_tail"] == "…ef99"
    assert "api_key" not in view
    assert "sk-secret-abcdef99" not in str(view)


def test_status_payload_marks_active_and_source(pool_file: Path) -> None:
    entry = add_entry("root", "deepseek/deepseek-chat", api_key="sk-p-11223344")
    payload = keypool.status_payload()
    assert payload["root"]["source"] == "keypool"
    assert payload["root"]["entry_id"] == entry["id"]
    assert payload["root"]["model"] == "deepseek/deepseek-chat"
    assert payload["flash"]["source"] == "env"
    views = {e["id"]: e for e in payload["entries"]}
    assert views[entry["id"]]["active"] is True
    serialized = str(payload)
    assert "sk-p-11223344" not in serialized


# ---------------------------------------------------------------------------
# test_entry：失败路径落状态（不真正联网——模型名非法直接在预检前失败）
# ---------------------------------------------------------------------------


def test_test_entry_records_failure(pool_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = add_entry("root", "definitely/not-a-real-model-xyz", api_key="sk-x-12345678")

    class _Boom:
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("401 Authentication Fails")

    # 桩掉预检：不联网，只验证"结果落盘 + 视图掩码"这段逻辑
    monkeypatch.setattr(
        "Ben.interface.cli.scan_setup.preflight_model_connection", _Boom()
    )
    result = run_entry_test(entry["id"])
    assert result["status"] == "fail"
    assert "401" in result["detail"]
    stored = read_pool()["entries"][0]
    assert stored["last_status"] == "fail"
    assert stored["last_error"] and "401" in stored["last_error"]
    assert stored["last_checked_at"]


def test_test_entry_unknown_id(pool_file: Path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        run_entry_test("nope")


def test_test_entry_success_records_ok(pool_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = add_entry("root", "deepseek/deepseek-chat", api_key="sk-ok-12345678")

    async def _fine(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "Ben.interface.cli.scan_setup.preflight_model_connection", _fine
    )
    result = run_entry_test(entry["id"])
    assert result["status"] == "ok"
    assert read_pool()["entries"][0]["last_status"] == "ok"
    assert read_pool()["entries"][0]["last_error"] is None
