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

import contextlib
import logging
import os
import uuid
from typing import Any, cast

from agents.sandbox.errors import ExposedPortUnavailableError
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxSession,
    _build_docker_volume_mounts,
    _docker_port_key,
    _manifest_requires_fuse,
    _manifest_requires_sys_admin,
)
from agents.sandbox.session.sandbox_session import SandboxSession
from agents.sandbox.types import ExposedPortEndpoint
from docker import errors as docker_errors  # type: ignore[import-untyped, unused-ignore]
from docker.models.containers import Container  # type: ignore[import-untyped, unused-ignore]
from docker.types import LogConfig  # type: ignore[import-untyped, unused-ignore]
from docker.types import Mount as DockerSDKMount  # type: ignore[import-untyped, unused-ignore]
from docker.utils import parse_repository_tag  # type: ignore[import-untyped, unused-ignore]

logger = logging.getLogger(__name__)


_SANDBOX_NETWORK_ENV = "BEN_DOCKER_SANDBOX_NETWORK"
_SANDBOX_DEVICES_ENV = "BEN_DOCKER_SANDBOX_DEVICES"


def _sandbox_network() -> str | None:
    value = os.environ.get(_SANDBOX_NETWORK_ENV, "").strip()
    return value or None


def _apply_sandbox_network(create_kwargs: dict[str, Any]) -> None:
    """Join the sandbox to a dedicated user-defined network.

    On a non-default network the DNS resolver, IPAM, and isolation policy are
    ours, which is what makes sandbox traffic separable from host traffic.
    Published ports are dropped: they would join the default bridge and publish
    on 0.0.0.0, contradicting the isolation this network exists to provide.
    """
    network = _sandbox_network()
    if network:
        create_kwargs["network"] = network
        create_kwargs.pop("ports", None)


def _sandbox_devices() -> list[str]:
    raw = os.environ.get(_SANDBOX_DEVICES_ENV, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _apply_sandbox_devices(create_kwargs: dict[str, Any]) -> None:
    """Pass the host devices listed in ``BEN_DOCKER_SANDBOX_DEVICES`` through.

    Comma-separated device paths, each mapped to the same path in the container
    with ``rwm`` access. A tunnel brought up inside the sandbox (OpenVPN,
    Tailscale) needs ``/dev/net/tun``: Docker's default device cgroup denies it
    even with ``NET_ADMIN``. Unset means no extra devices.
    """
    devices = _sandbox_devices()
    if not devices:
        return
    existing = [str(item) for item in (create_kwargs.get("devices") or [])]
    for path in devices:
        spec = f"{path}:{path}:rwm"
        if spec not in existing and path not in existing:
            existing.append(spec)
    create_kwargs["devices"] = existing


def _apply_resource_limits(create_kwargs: dict[str, Any]) -> None:
    """Apply optional cgroup caps (mem/shm/cpu/pids) from the environment."""
    mem_limit = os.environ.get("BEN_SANDBOX_MEM_LIMIT", "").strip()
    if mem_limit:
        create_kwargs["mem_limit"] = mem_limit
    shm_size = os.environ.get("BEN_SANDBOX_SHM_SIZE", "").strip()
    if shm_size:
        create_kwargs["shm_size"] = shm_size
    cpus = os.environ.get("BEN_SANDBOX_CPUS", "").strip()
    if cpus:
        with contextlib.suppress(ValueError, OverflowError):
            nano_cpus = int(float(cpus) * 1_000_000_000)
            if 0 < nano_cpus <= 2**63 - 1:
                create_kwargs["nano_cpus"] = nano_cpus
    pids_limit = os.environ.get("BEN_SANDBOX_PIDS_LIMIT", "").strip()
    if pids_limit:
        with contextlib.suppress(ValueError):
            create_kwargs["pids_limit"] = int(pids_limit)


def _apply_run_labels(create_kwargs: dict[str, Any]) -> None:
    run_id = os.getenv("BEN_RUN_ID")
    if not run_id:
        return
    labels = create_kwargs.setdefault("labels", {})
    if not isinstance(labels, dict):
        return
    labels["ben-run-id"] = run_id
    run_type = os.getenv("BEN_RUN_TYPE")
    if run_type:
        labels["ben-run-type"] = run_type


def _sandbox_security_kwargs(manifest: Manifest | None) -> dict[str, Any]:
    """按 manifest 的挂载策略决定容器需要的设备与权限。

    - FUSE 类挂载（fuse / mountpoint / rclone-fuse）需要 ``/dev/fuse``；
    - NFS / S3 类挂载只需要 ``SYS_ADMIN``；
    - 两者都必须解除 apparmor 限制，否则挂载被拒。

    返回值直接 merge 进 ``containers.run(**kwargs)``。
    """
    if _manifest_requires_fuse(manifest):
        return {
            "devices": ["/dev/fuse"],
            "cap_add": ["SYS_ADMIN"],
            "security_opt": ["apparmor:unconfined"],
        }
    if _manifest_requires_sys_admin(manifest):
        return {
            "cap_add": ["SYS_ADMIN"],
            "security_opt": ["apparmor:unconfined"],
        }
    return {}


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


class BenDockerSandboxSession(DockerSandboxSession):
    """Resolve exposed ports on the sandbox network instead of 127.0.0.1.

    With ``BEN_DOCKER_SANDBOX_NETWORK`` the agent must reach an exposed port at
    the container's address on that network; the default published-port
    mapping is deliberately not created.
    """

    sandbox_network: str = ""

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            self._container.reload()
        except docker_errors.APIError as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={
                    "backend": "docker",
                    "detail": "container_reload_failed",
                    "network": self.sandbox_network,
                },
                cause=e,
            ) from e

        attrs = getattr(self._container, "attrs", {}) or {}
        networks = attrs.get("NetworkSettings", {}).get("Networks", {})
        endpoint = networks.get(self.sandbox_network) or {}
        ip = endpoint.get("IPAddress") or endpoint.get("GlobalIPv6Address")
        if not isinstance(ip, str) or not ip:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={
                    "backend": "docker",
                    "detail": "container_not_on_network",
                    "network": self.sandbox_network,
                },
            )
        host = f"[{ip}]" if ":" in ip else ip
        return ExposedPortEndpoint(host=host, port=port, tls=False)


class BenDockerSandboxClient(DockerSandboxClient):
    # 宿主机目录 bind-mount 进容器，由 docker backend 在 ``create()`` 前写入。
    # 每项为 ``{source, target, read_only}``。
    ben_bind_mounts: list[dict[str, Any]] | None = None

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
            create_kwargs.update(_sandbox_security_kwargs(manifest))
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

        _apply_sandbox_network(create_kwargs)
        _apply_sandbox_devices(create_kwargs)
        _apply_resource_limits(create_kwargs)
        _apply_log_limits(create_kwargs)
        _apply_run_labels(create_kwargs)

        # Ben injection：本地源码树 bind-mount，浅的排前面，嵌套的后挂载
        # 使其覆盖在父树之上。
        bind_mounts = self.ben_bind_mounts or ()
        if bind_mounts:
            mounts = create_kwargs.setdefault("mounts", [])
            for spec in sorted(bind_mounts, key=lambda s: str(s["target"]).count("/")):
                mounts.append(
                    DockerSDKMount(
                        target=spec["target"],
                        source=spec["source"],
                        type="bind",
                        read_only=spec.get("read_only", False),
                    )
                )

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

    async def create(self, **kwargs: Any) -> SandboxSession:
        session = await super().create(**kwargs)
        network = _sandbox_network()
        inner = session._inner  # noqa: SLF001
        if network and isinstance(inner, DockerSandboxSession):
            inner.__class__ = BenDockerSandboxSession
            cast("BenDockerSandboxSession", inner).sandbox_network = network
        return session

