"""BenDockerSandboxClient — 保留镜像 ENTRYPOINT，追加 NET_ADMIN/NET_RAW 与 host-gateway。

对齐 strix 的 runtime/docker_client.py。SDK 的
``DockerSandboxClient._create_container`` 没有暴露扩展 create_kwargs 的
钩子，这里子类化并按 SDK 源码重写方法体，差异三处：

1. 去掉 SDK 的 ``entrypoint=["tail"]`` 覆盖，改为以
   ``command=["tail", "-f", "/dev/null"]`` 保活——这样镜像的
   ``docker-entrypoint.sh`` 才会真正执行；没有它 caido-cli 不会在容器内
   启动，``bootstrap_caido`` 只能对着死端口重试。
2. 追加 NET_ADMIN/NET_RAW（``nmap -sS`` 等 raw-socket 工具需要）。
3. 加 ``host.docker.internal`` → host-gateway，agent 可达宿主机服务。

另加容器 json-file 日志上限（默认 50m×3，``BEN_SANDBOX_LOG_MAX_SIZE`` /
``BEN_SANDBOX_LOG_MAX_FILE`` 可调），防止沙箱内失控进程写满宿主机磁盘。

锁定 SDK 当前版本的方法体；升级 SDK 需要重新比对父类实现。
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    _build_docker_volume_mounts,
    _docker_port_key,
    _manifest_requires_fuse,
    _manifest_requires_sys_admin,
)
from docker.models.containers import Container  # type: ignore[import-untyped, unused-ignore]
from docker.types import LogConfig  # type: ignore[import-untyped, unused-ignore]
from docker.utils import parse_repository_tag  # type: ignore[import-untyped, unused-ignore]


logger = logging.getLogger(__name__)


def _apply_log_limits(create_kwargs: dict[str, Any]) -> None:
    """给容器 json-file 日志加上限（默认开启；设 0/off 退回 docker 默认）。"""
    max_size = os.environ.get("BEN_SANDBOX_LOG_MAX_SIZE", "50m").strip()
    if max_size.lower() in ("0", "off", "none", "unlimited"):
        return
    max_file = os.environ.get("BEN_SANDBOX_LOG_MAX_FILE", "3").strip() or "3"
    create_kwargs["log_config"] = LogConfig(
        type=LogConfig.types.JSON,
        config={"max-size": max_size, "max-file": max_file},
    )


class BenDockerSandboxClient(DockerSandboxClient):
    async def _create_container(
        self,
        image: str,
        *,
        manifest: Manifest | None = None,
        exposed_ports: tuple[int, ...] = (),
        session_id: uuid.UUID | None = None,
    ) -> Container:
        # ----- BEGIN VERBATIM COPY of DockerSandboxClient._create_container -----
        if not self.image_exists(image):
            repo, tag = parse_repository_tag(image)
            self.docker_client.images.pull(repo, tag=tag or None, all_tags=False)

        assert self.image_exists(image)
        environment: dict[str, str] | None = None
        if manifest:
            environment = await manifest.environment.resolve()
        # Ben delta：去掉 entrypoint 覆盖，用 command 保活，让镜像的
        # ENTRYPOINT（docker-entrypoint.sh）先跑 caido-cli 启动、CA 信任、
        # 系统级代理环境变量写入，最后 exec "$@" 变成 exec tail -f /dev/null。
        create_kwargs: dict[str, Any] = {
            "image": image,
            "detach": True,
            "command": ["tail", "-f", "/dev/null"],
            "environment": environment,
        }
        if manifest is not None:
            docker_mounts = _build_docker_volume_mounts(
                manifest,
                session_id=session_id,
            )
            if docker_mounts:
                create_kwargs["mounts"] = docker_mounts
            if _manifest_requires_fuse(manifest):
                create_kwargs.update(
                    devices=["/dev/fuse"],
                    cap_add=["SYS_ADMIN"],
                    security_opt=["apparmor:unconfined"],
                )
            elif _manifest_requires_sys_admin(manifest):
                create_kwargs.update(
                    cap_add=["SYS_ADMIN"],
                    security_opt=["apparmor:unconfined"],
                )
        if exposed_ports:
            create_kwargs["ports"] = {
                _docker_port_key(port): ("127.0.0.1", None) for port in exposed_ports
            }
        # ----- END VERBATIM COPY -----

        # Ben injections — 追加而非覆盖，保住 FUSE/SYS_ADMIN 分支。
        cap_add = create_kwargs.setdefault("cap_add", [])
        if not isinstance(cap_add, list):
            cap_add = list(cap_add)
            create_kwargs["cap_add"] = cap_add
        for cap in ("NET_ADMIN", "NET_RAW"):
            if cap not in cap_add:
                cap_add.append(cap)

        extra_hosts = create_kwargs.setdefault("extra_hosts", {})
        extra_hosts["host.docker.internal"] = "host-gateway"

        _apply_log_limits(create_kwargs)

        logger.debug(
            "Creating sandbox container: image=%s caps=%s exposed_ports=%s",
            image,
            cap_add,
            list(exposed_ports),
        )
        container = self.docker_client.containers.create(**create_kwargs)
        logger.info(
            "Sandbox container created: id=%s image=%s",
            container.short_id if hasattr(container, "short_id") else "?",
            image,
        )
        return container
