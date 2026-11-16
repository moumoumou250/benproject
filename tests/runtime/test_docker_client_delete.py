"""BenDockerSandboxClient 的容器销毁路径。

历史背景：Ben 曾在 ``delete()`` 里先做一次 best-effort ``kill``，再委托给
SDK 删除容器；那段覆盖实现后来被移除，销毁完全交给 SDK。这里锁定当前
契约——**不要**重新引入会吞掉异常的自定义 ``delete``，否则真实的编程
错误会被静默。

``_sandbox_security_kwargs`` 的设备/权限策略见
``test_docker_client_devices.py``。
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents.sandbox.sandboxes.docker import DockerSandboxClient

from Ben.runtime.docker_client import BenDockerSandboxClient


def test_client_does_not_override_delete() -> None:
    """销毁逻辑必须原样走 SDK，不引入吞异常的自定义实现。"""
    assert BenDockerSandboxClient.delete is DockerSandboxClient.delete


@pytest.mark.asyncio
async def test_delete_delegates_to_the_sdk() -> None:
    """调用 delete 时确实进到 SDK 的实现（而不是本地分支）。"""
    client = BenDockerSandboxClient.__new__(BenDockerSandboxClient)
    client.docker_client = MagicMock()
    session = MagicMock(name="sandbox_session")

    with patch.object(
        DockerSandboxClient, "delete", new=AsyncMock(return_value=session)
    ) as sdk_delete:
        result = await client.delete(session)

    assert result is session
    sdk_delete.assert_awaited_once()


def test_delete_signature_matches_the_sdk() -> None:
    """签名漂移会让调用方（runner / session_manager）在运行期才炸。"""
    assert inspect.signature(BenDockerSandboxClient.delete) == inspect.signature(
        DockerSandboxClient.delete
    )
