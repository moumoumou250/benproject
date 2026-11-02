"""沙箱容器的设备与权限按 manifest 的挂载策略决定。

FUSE 类挂载需要 /dev/fuse；NFS/S3 类只需要 SYS_ADMIN；两者都要解除
apparmor 限制。默认（无 manifest 或普通 bind mount）不加任何特权。
"""

from __future__ import annotations

from typing import Any

import pytest

from Ben.runtime import docker_client


def _security(monkeypatch: pytest.MonkeyPatch, *, fuse: bool, sys_admin: bool) -> dict[str, Any]:
    monkeypatch.setattr(docker_client, "_manifest_requires_fuse", lambda _m: fuse)
    monkeypatch.setattr(docker_client, "_manifest_requires_sys_admin", lambda _m: sys_admin)
    return docker_client._sandbox_security_kwargs(object())


def test_plain_manifest_gets_no_extra_privileges(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _security(monkeypatch, fuse=False, sys_admin=False) == {}


def test_no_manifest_gets_no_extra_privileges() -> None:
    assert docker_client._sandbox_security_kwargs(None) == {}


def test_fuse_manifest_maps_dev_fuse_and_sys_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = _security(monkeypatch, fuse=True, sys_admin=False)

    assert kwargs["devices"] == ["/dev/fuse"]
    assert kwargs["cap_add"] == ["SYS_ADMIN"]
    assert kwargs["security_opt"] == ["apparmor:unconfined"]


def test_sys_admin_only_manifest_gets_no_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = _security(monkeypatch, fuse=False, sys_admin=True)

    assert "devices" not in kwargs
    assert kwargs["cap_add"] == ["SYS_ADMIN"]
    assert kwargs["security_opt"] == ["apparmor:unconfined"]


def test_fuse_wins_when_both_are_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """FUSE 分支优先，且不会重复叠加权限项。"""
    kwargs = _security(monkeypatch, fuse=True, sys_admin=True)

    assert kwargs["devices"] == ["/dev/fuse"]
    assert kwargs["cap_add"] == ["SYS_ADMIN"]
    assert kwargs["security_opt"] == ["apparmor:unconfined"]
