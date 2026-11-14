"""最小 Docker 执行器（第二阶段，暂不接入大模型）。

职责：
1. 检查 Docker 守护进程是否可用（复用 runtime/docker_connection.py 的
   连接 + ping + 指数退避重试逻辑）；
2. 创建/复用一个专用 Docker 网络（默认不使用宿主机网络）；
3. 启动或复用一个常驻执行容器（默认 strix 沙盒镜像，sleep 常驻），并覆盖
   镜像内置 entrypoint 以跳过其 Caido/全局代理初始化；不挂载宿主机敏感目录、
   不挂载 Docker socket、不使用特权容器；
4. 在容器内部执行允许的命令，返回标准输出、标准错误、退出码；
5. 处理命令超时（超时后重启容器，确保命令被真正终止）与执行异常；
6. 明确的生命周期：默认任务结束停止并删除执行容器。

注意：Docker 容器不是绝对安全的沙箱。这里通过"专用网络 + 不挂载 + 非特权
+ 授权目标白名单（在工具层校验）"来收敛风险，而不是假设容器天然安全。

用固定命令做独立验证（不接模型）：
    .venv/Scripts/python.exe -m Ben.runtime.docker_executor
"""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Sequence

# 让项目根可导入，以便按 Ben.runtime.docker_connection 绝对导入（无论从哪个 CWD 运行）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from Ben.runtime.docker_connection import (  # noqa: E402
    DockerConnectionError,
    connect_docker,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """一条命令在容器内的真实执行结果。

    模型/调用方只能依据这里的字段判断"操作是否真的发生、结果如何"，
    不能凭模型自己的措辞认定命令已执行成功。
    """

    command: Sequence[str]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    container: str

    @property
    def ok(self) -> bool:
        """退出码为 0 且未超时，才算命令成功。"""
        return not self.timed_out and self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 3),
            "container": self.container,
            "ok": self.ok,
        }


class DockerExecutor:
    """在受控 Docker 容器内执行命令的最小执行器。"""

    def __init__(
        self,
        *,
        image: str = "ghcr.io/usestrix/strix-sandbox:1.4.0",
        network: str = "benptnet",
        container_name: str = "benpt-executor",
        keep_alive_command: Sequence[str] = ("sleep", "infinity"),
        # strix 沙盒基于 Kali，自带 curl，无需再安装；保留参数以便需要时补充工具。
        setup_commands: Sequence[Sequence[str]] = (),
        # 空序列 = 清空镜像内置 entrypoint，跳过 Caido/代理初始化（见 ensure_container）。
        # 显式传 None 则保留镜像默认 entrypoint。
        entrypoint: Sequence[str] | None = (),
        auto_remove: bool = True,
        mem_limit: str = "512m",
        cpus: float = 1.0,
    ) -> None:
        self.image = image
        self.network = network
        self.container_name = container_name
        self.keep_alive_command = list(keep_alive_command)
        self.setup_commands = [list(c) for c in setup_commands]
        self.entrypoint = None if entrypoint is None else list(entrypoint)
        self.auto_remove = auto_remove
        self.mem_limit = mem_limit
        self.cpus = cpus
        self._client: Any | None = None
        self._container: Any | None = None
        self._network_created = False

    # ------------------------------------------------------------------
    # 连接与基础环境
    # ------------------------------------------------------------------
    def connect(self) -> Any:
        # Docker客户端对象为空，尝试连接
        if self._client is None:
            try:
                self._client = connect_docker()
            except DockerConnectionError as exc:
                raise RuntimeError(f"Docker 不可用：{exc}") from exc
            logger.info("Docker 连接成功：%s", self._client.version().get("Version"))
        return self._client

    def ensure_image(self) -> None:
        """镜像不存在则拉取（给出明确提示，避免静默卡住）。"""
        client = self.connect()
        if client.images.list(name=self.image):
            return
        repo_name, _, tag = self.image.partition(":")
        tag = tag or "latest"
        logger.info("拉取镜像 %s（首次可能需要一些时间）...", self.image)
        try:
            client.images.pull(repo_name, tag=tag)
        except Exception as exc:  # noqa: BLE001 - 转为明确错误
            raise RuntimeError(f"拉取镜像 {self.image} 失败：{exc}") from exc

    def ensure_network(self) -> Any:
        """创建专用网络（若不存在）。默认 bridge 驱动，与宿主机网络隔离。"""
        client = self.connect()
        existing = client.networks.list(names=[self.network])
        if existing:
            return existing[0]
        net = client.networks.create(self.network, driver="bridge", check_duplicate=True)
        self._network_created = True
        logger.info("已创建专用网络：%s", self.network)
        return net

    def ensure_container(self) -> Any:
        # 容器不为空，刷新容器状态
        if self._container is not None:
            self._container.reload()
            # 容器状态为 running，直接返回
            if self._container.status == "running":
                return self._container
        # 容器不正常，执行下方创建容器逻辑
        client = self.connect()
        self.ensure_image()
        self.ensure_network()

        # 查询所有容器  filters正则匹配容器名
        matches = client.containers.list(all=True, filters={"name": f"^{self.container_name}$"})
        if matches:
            # 获取第一个对象，刷新容器状态；没运行则进行启动
            container = matches[0]
            container.reload()
            if container.status != "running":
                container.start()
            # container.attrs是docker的属性字典
            """
            本项目`benpt-executor` 容器的真实`attrs`
            {
            "benptnet": {
                "NetworkID": "b1deee7493ec...",
                "EndpointID": "f75892b96f4b...",
                "Gateway": "172.21.0.1",
                "IPAddress": "172.21.0.3",
                "MacAddress": "6e:93:d6:06:ed:cf",
                "IPPrefixLen": 16,
                "DNSNames": ["benpt-executor", "6892db43b2f5"]
                }
            }
            当前网络不在容器已加入网络中，尝试加入。
            """
            if self.network not in container.attrs.get("NetworkSettings", {}).get("Networks", {}):
                try:
                    client.networks.get(self.network).connect(container)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("容器加入网络 %s 失败：%s", self.network, exc)
        # 容器不存在，创建新容器
        else:
            # 安全收敛：不挂载宿主机目录、不挂 docker socket、非特权、限制内存/CPU。
            create_kwargs: dict[str, Any] = {
                "command": self.keep_alive_command,
                "name": self.container_name,
                "network": self.network,
                "detach": True,
                "stdin_open": True,
                "tty": True,
                "mem_limit": self.mem_limit,
                "nano_cpus": int(self.cpus * 1e9),
                "privileged": False,
            }
            # entrypoint是 Docker启动的默认入口
            # 不为空，则指定了entrypoint，否则使用默认entrypoint
            if self.entrypoint is not None:
                # 空列表清空内置 entrypoint，跳过 strix 镜像的 Caido/代理初始化。
                create_kwargs["entrypoint"] = self.entrypoint
            container = client.containers.create(self.image, **create_kwargs)
            container.start()
            logger.info(
                "已启动执行容器 %s（image=%s, network=%s, 非特权/无挂载, entrypoint=%s）",
                self.container_name, self.image, self.network, self.entrypoint,
            )

        self._container = container

        # 一次性初始化（如安装 curl）。失败不致命：连通性检查仍可用 wget。
        for setup_cmd in self.setup_commands:
            setup_res = self._exec(setup_cmd, timeout=120)
            if not setup_res.ok:
                logger.warning(
                    "容器初始化命令 %s 失败（exit=%s）：%s",
                    setup_cmd, setup_res.exit_code, setup_res.stderr.strip(),
                )
        return container

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------
    def _exec(self, command: Sequence[str], *, timeout: float) -> CommandResult:
        """实际在容器内执行（内部，不做容器就绪检查）。"""
        container = self._container
        started = time.monotonic()
        cmd_list = list(command)

        def _run() -> tuple[int | None, bytes | None, bytes | None]:
            # demux=True：output 为 (stdout, stderr) 二元组，便于分别返回。
            result = container.exec_run(cmd_list, demux=True)
            stdout, stderr = result.output if result.output else (None, None)
            return result.exit_code, stdout, stderr

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_run)
                try:
                    exit_code, stdout, stderr = future.result(timeout=timeout)
                except FutureTimeout:
                    duration = time.monotonic() - started
                    logger.error("命令超时（%.1fs）：%s，重启容器以终止它", timeout, cmd_list)
                    # Docker API 无法单独杀掉一个 exec 进程，重启容器可确保命令终止。
                    self._restart_container()
                    return CommandResult(
                        command=cmd_list,
                        exit_code=None,
                        stdout="",
                        stderr=f"命令在 {timeout:.0f} 秒后超时，已重启容器终止执行",
                        timed_out=True,
                        duration_seconds=duration,
                        container=self.container_name,
                    )
        except Exception as exc:  # noqa: BLE001 - 执行通道异常要明确暴露
            duration = time.monotonic() - started
            return CommandResult(
                command=cmd_list,
                exit_code=None,
                stdout="",
                stderr=f"执行异常：{type(exc).__name__}: {exc}",
                timed_out=False,
                duration_seconds=duration,
                container=self.container_name,
            )

        return CommandResult(
            command=cmd_list,
            exit_code=exit_code,
            stdout=(stdout or b"").decode("utf-8", errors="replace"),
            stderr=(stderr or b"").decode("utf-8", errors="replace"),
            timed_out=False,
            duration_seconds=time.monotonic() - started,
            container=self.container_name,
        )

    def run(self, command: Sequence[str], *, timeout: float = 30.0) -> CommandResult:
        """对外执行入口：确保容器就绪后在容器内执行命令。"""
        self.ensure_container()
        logger.info("容器内执行：%s（timeout=%ss）", list(command), timeout)
        return self._exec(command, timeout=timeout)

    def _restart_container(self) -> None:
        """重启执行容器（超时后用于终止命令），并重跑初始化。"""
        if self._container is None:
            return
        try:
            self._container.restart(timeout=30)
            for setup_cmd in self.setup_commands:
                self._exec(setup_cmd, timeout=120)
        except Exception as exc:  # noqa: BLE001
            logger.error("重启容器失败：%s", exc)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def cleanup(self) -> None:
        """任务结束：默认停止并删除执行容器（auto_remove=True）。"""
        client = self.connect()
        if self.auto_remove:
            matches = client.containers.list(all=True, filters={"name": f"^{self.container_name}$"})
            for container in matches:
                try:
                    container.remove(force=True)
                    logger.info("已删除执行容器：%s", container.name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("删除容器 %s 失败：%s", container.name, exc)
        self._container = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def remove_network(self) -> None:
        """显式删除专用网络（仅在确认没有其它容器使用时调用，如靶场也已清理）。"""
        client = self.connect()
        try:
            net = client.networks.get(self.network)
            net.remove()
            logger.info("已删除专用网络：%s", self.network)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除网络 %s 失败：%s", self.network, exc)

    def __enter__(self) -> DockerExecutor:
        self.ensure_container()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.cleanup()


# ----------------------------------------------------------------------
# 独立自检：固定命令验证（不接模型）
# ----------------------------------------------------------------------
def _selftest() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print("== Docker 执行器独立自检（固定命令，不接模型）==")
    executor = DockerExecutor()
    try:
        executor.ensure_container()

        # [1] 正常命令：stdout + 退出码 0
        r1 = executor.run(["echo", "executor-round-trip-ok"])
        print(f"[1] echo -> exit={r1.exit_code}, stdout={r1.stdout.strip()!r}, ok={r1.ok}")
        if not r1.ok or r1.stdout.strip() != "executor-round-trip-ok":
            print("[FAIL] 正常命令未返回预期结果")
            return 1

        # [2] 非零退出码 + stderr
        r2 = executor.run(["sh", "-c", "echo to-stderr 1>&2; exit 7"])
        print(f"[2] 非零退出 -> exit={r2.exit_code}, stderr={r2.stderr.strip()!r}, ok={r2.ok}")
        if r2.exit_code != 7 or "to-stderr" not in r2.stderr:
            print("[FAIL] 退出码/stderr 未正确返回")
            return 1

        # [3] 超时处理：sleep 超过给定 timeout
        r3 = executor.run(["sleep", "10"], timeout=3)
        print(f"[3] 超时 -> timed_out={r3.timed_out}, exit={r3.exit_code}, stderr={r3.stderr.strip()!r}")
        if not r3.timed_out:
            print("[FAIL] 超时未被识别")
            return 1

        # [4] 超时后容器仍可正常执行（证明已重启恢复）
        r4 = executor.run(["echo", "recovered-after-timeout"])
        print(f"[4] 超时恢复 -> exit={r4.exit_code}, stdout={r4.stdout.strip()!r}, ok={r4.ok}")
        if not r4.ok or r4.stdout.strip() != "recovered-after-timeout":
            print("[FAIL] 超时后容器未恢复")
            return 1

        # [5] curl 由 strix 沙盒镜像自带，可直接使用
        r5 = executor.run(["curl", "--version"])
        print(f"[5] curl 可用 -> exit={r5.exit_code}, 首行={r5.stdout.splitlines()[0] if r5.stdout else ''!r}")
        if r5.exit_code != 0:
            print("[FAIL] 容器内 curl 不可用")
            return 1

        print("ALL OK - Docker 执行器：启动/复用、执行、退出码、stdout/stderr、超时与恢复均正常")
        return 0
    finally:
        executor.cleanup()


if __name__ == "__main__":
    sys.exit(_selftest())
