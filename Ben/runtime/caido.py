"""Caido 代理的 bootstrap 与并发句柄（对齐 strix 的 runtime/caido_*.py）。

Caido CLI 作为容器内 sidecar 运行，监听容器内 ``127.0.0.1:48080``
（由镜像的 docker-entrypoint.sh 启动）。bootstrap 流程：
先在容器内 ``session.exec()`` curl 拿 guest token，再用宿主机侧
``caido_sdk_client.Client`` 连 runtime 暴露的端口做后续 GraphQL 调用。

``CaidoBootstrapHandle`` 包装进行中的 bootstrap 任务：session 起来后
立刻返回，首个 proxy 工具调用时才解析 client；bootstrap 失败对所有
``get()`` 调用者抛原始异常一次，proxy 工具降级为"client 不可用"结果，
而不是在启动阶段直接搞挂扫描。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from agents.sandbox.session import BaseSandboxSession
    from caido_sdk_client import Client


logger = logging.getLogger(__name__)

# 容器内 Caido sidecar 端口（与镜像 entrypoint 的 caido-cli 监听一致）
CONTAINER_CAIDO_PORT = 48080


_LOGIN_AS_GUEST_BODY = (
    '{"query":"mutation LoginAsGuest { loginAsGuest { token { accessToken } } }"}'
)


async def _login_as_guest(
    session: BaseSandboxSession,
    *,
    container_url: str,
    attempts: int = 10,
) -> str:
    """``session.exec`` curl 获取 guest token；重试循环兼作 Caido 就绪探针。"""
    last_err: str | None = None
    for i in range(1, attempts + 1):
        result = await session.exec(
            "curl",
            "-fsS",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            _LOGIN_AS_GUEST_BODY,
            f"{container_url}/graphql",
            timeout=15,
        )
        if result.ok():
            try:
                payload = json.loads(result.stdout)
                token = (
                    payload.get("data", {})
                    .get("loginAsGuest", {})
                    .get("token", {})
                    .get("accessToken")
                )
                if token:
                    return str(token)
                last_err = f"loginAsGuest returned no token: {payload}"
            except json.JSONDecodeError as exc:
                last_err = f"unparseable response: {exc}: {result.stdout!r}"
        else:
            stderr = result.stderr.decode("utf-8", errors="replace")[:200]
            last_err = f"curl exit {result.exit_code}: {stderr}"
        logger.debug("loginAsGuest attempt %d/%d failed: %s", i, attempts, last_err)
        await asyncio.sleep(min(2.0 * i, 8.0))

    raise RuntimeError(f"loginAsGuest failed after {attempts} attempts: {last_err}")


async def bootstrap_caido(
    session: BaseSandboxSession,
    *,
    host_url: str,
    container_url: str,
) -> Client:
    """连接容器内 Caido sidecar 并选中一个全新临时项目。"""
    # caido SDK（及其生成的 GraphQL schema）导入很慢，且只在真正
    # bootstrap 沙箱时才需要，所以放在函数内而不是模块顶层。
    from caido_sdk_client import Client, TokenAuthOptions
    from caido_sdk_client.types import CreateProjectOptions

    logger.info("Bootstrapping Caido client (host=%s, container=%s)", host_url, container_url)

    access_token = await _login_as_guest(session, container_url=container_url)

    client = Client(host_url, auth=TokenAuthOptions(token=access_token))
    try:
        # connect() 也在 guard 内：此处的 cancellation（扫描收尾时 bootstrap
        # 仍在途）否则会留下半连接的 transport。
        await client.connect()
        project = await client.project.create(
            CreateProjectOptions(name="sandbox", temporary=True),
        )
        await client.project.select(project.id)
    except BaseException:
        with contextlib.suppress(Exception):
            await client.aclose()
        raise
    logger.info("Caido project selected: %s", project.id)
    return client


class CaidoBootstrapHandle:
    """bootstrap 完成后解析出已连接的 Caido client。

    bootstrap 失败对所有 ``get()`` 调用者抛原始异常（一次）；proxy 工具
    捕获后降级为"client 不可用"结果，不让失败在启动阶段杀死扫描。
    """

    def __init__(self, task: asyncio.Task[Client]) -> None:
        self._task = task

    async def get(self) -> Client:
        """等待 bootstrap 完成并返回 client。

        用 shield 包住：某个调用者的 cancellation（如工具超时）不会
        取消共享的 bootstrap 任务。
        """
        return await asyncio.shield(self._task)

    def peek(self) -> Client | None:
        """bootstrap 已干净完成时返回 client，否则 None。"""
        if self._task.done() and not self._task.cancelled() and self._task.exception() is None:
            return self._task.result()
        return None

    async def aclose(self) -> None:
        """取消在途的 bootstrap，或关闭已完成的 client。"""
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            return
        client = self.peek()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
