"""目标预检：启动扫描之前先证明"这个目标真的可达"。

对齐 sapper 的 ``sapper validate``——只读、不写任何文件、退出码即结论：

    0  全部目标通过（可以启动扫描）
    1  至少一个目标不可达 / 未授权 / 写法非法

为什么需要它：``strix_runs/117-72-199-2-3000_*`` 那次运行里，靶场
``http://172.19.0.2`` 根本没起来，直连超时、Caido 返回 502，整轮花掉
445 秒（其中 300 秒在 ``wait_for_agents`` 空等）最后 0 个发现。模型不是
不会打，是**没有人在开跑前确认过目标还在**。

三层探测，缺一不可：

  authorize  主机是否在 ``.env`` 的 ``ALLOWED_TARGETS`` 内 —— 不在的话
             工具层会逐条拒绝命令，扫描必然空转
  probe-host 宿主机视角：DNS → TCP → HTTP（不走环境代理）
  probe-      沙箱视角：在 ``benptnet`` 容器内 curl。宿主可达 ≠ 容器可达，
  container  ``172.19.0.2`` 这类地址只有容器视角的结论才算数

用法::

    ben preflight http://127.0.0.1:3000/
    ben preflight -t http://10.0.0.5:8080 -t https://staging.example.com --json
    ben preflight --target-list targets.txt --timeout 3
    ben preflight 10.0.0.5 --port 8080 --port 9000
    ben preflight http://172.19.0.2:3000 --no-docker     # 只做宿主机探测
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# 本机别名组：ALLOWED_TARGETS 写 localhost，实际探测可能是 host.docker.internal
# （容器内回指宿主机），工具层的 _LOCAL_HOSTS 也把这一组一律放行。
_LOCAL_HOST_ALIASES = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
)

_DEFAULT_IP_PORTS = (80, 443)
_CURL_META = "__BEN_META__"

# curl 退出码 → 人话（只覆盖预检会遇到的几种）
_CURL_ERRORS = {
    5: "代理解析失败",
    6: "DNS 解析失败",
    7: "TCP 连接被拒绝",
    18: "传输提前中断",
    28: "连接或读取超时",
    35: "TLS 握手失败",
    52: "服务端空回复",
    56: "接收数据失败",
    60: "证书校验失败（预检已用 -k 忽略，仍失败说明协议层有问题）",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    """单项检查结果。``status`` 取 ok / fail / skip；skip 既不算通过也不算失败。"""

    name: str
    status: str
    detail: str
    hint: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "elapsed_seconds": round(self.elapsed, 3),
        }


@dataclass
class TargetReport:
    """单个目标的预检结论。"""

    target: str
    kind: str = ""
    url: str = ""
    probes: list[Probe] = field(default_factory=list)

    def add(self, probe: Probe) -> None:
        self.probes.append(probe)

    @property
    def fatal(self) -> bool:
        """目标写法解析不了：连类型都定不下来，后续探测无从谈起。"""
        return any(p.status == "fail" and p.name == "classify" for p in self.probes)

    def find(self, name: str) -> Probe | None:
        return next((p for p in self.probes if p.name == name), None)

    @property
    def authorized(self) -> bool:
        """是否落在授权范围内。

        只回答"能不能扫"，不回答"通不通"。非网络目标不走主机白名单，
        ``authorize`` 是 skip，视为已授权。
        """
        probe = self.find("authorize")
        return probe is None or probe.status in ("ok", "skip")

    @property
    def ready(self) -> bool:
        """既能连通、又在授权范围内 —— 扫描可以开跑。"""
        return self.reachable and self.authorized

    @property
    def reachable(self) -> bool:
        """网络是否可达。与授权状态无关。

        以容器视角为准（那才是扫描端），容器探测被跳过时退回宿主机视角。
        非网络目标（本地目录/仓库）没有 probe-*，看类型专属检查的结论。
        """
        if self.fatal:
            return False
        container = self.find("probe-container")
        if container is not None and container.status != "skip":
            return container.ok
        host = self.find("probe-host")
        if host is not None:
            return host.ok
        for name in ("source", "repo"):
            probe = self.find(name)
            if probe is not None:
                return probe.ok
        return True  # 没有任何失败项

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "type": self.kind,
            "url": self.url,
            "reachable": self.reachable,
            "authorized": self.authorized,
            "ready": self.ready,
            "checks": [p.to_dict() for p in self.probes],
        }


# ---------------------------------------------------------------------------
# 目标 → 主机
# ---------------------------------------------------------------------------


def _host_of(target: str) -> str:
    """从任意目标写法里抠出主机名（小写，去端口/路径）。"""
    raw = (target or "").strip()
    if not raw:
        return ""
    if raw.startswith("git@"):
        rest = raw[4:]
        return rest.split(":", 1)[0].split("/", 1)[0].strip().lower()
    if "://" in raw:
        try:
            return (urlsplit(raw).hostname or "").strip().lower()
        except ValueError:
            return ""
    if "@" in raw:  # 省略协议的 ssh 写法 user@host:path
        rest = raw.rsplit("@", 1)[1]
        return rest.split(":", 1)[0].split("/", 1)[0].strip().lower()
    candidate = raw.split("/", 1)[0]
    if candidate.count(":") == 1:  # host:port；IPv6 冒号多于一个，不动
        candidate = candidate.split(":", 1)[0]
    candidate = candidate.strip("[]")
    if candidate and re.fullmatch(r"[A-Za-z0-9._:\-\[\]%]+", candidate):
        return candidate.lower()
    return ""


def _primary_host(info: dict[str, Any]) -> str:
    details = info.get("details") or {}
    for key in ("target_url", "target_repo", "target_spec", "target_ip", "target_path"):
        value = details.get(key)
        if isinstance(value, str) and value:
            host = _host_of(value)
            if host:
                return host
    return ""


def _authorized(host: str, allowed: tuple[str, ...]) -> tuple[bool, str]:
    """主机是否落在授权白名单内（与工具层闸门同一套判定口径）。"""
    allow_hosts = {h for h in (_host_of(item) for item in allowed) if h}
    shown = "、".join(sorted(allow_hosts)) or "（空）"
    if not host:
        return False, f"无法从目标中解析主机名；授权范围：{shown}"
    if host in allow_hosts:
        return True, f"{host} 在授权白名单内"
    if host in _LOCAL_HOST_ALIASES and allow_hosts & _LOCAL_HOST_ALIASES:
        return True, f"{host} 与本机别名（localhost/127.0.0.1/host.docker.internal）同属授权范围"
    return False, f"{host} 不在授权白名单内；当前授权范围：{shown}"


def _host_view_url(raw: str, rewritten: str) -> str:
    """宿主机视角该用哪个地址。

    ``build_targets_info`` 会把 localhost 改写成 host.docker.internal（容器内
    回指宿主机）。宿主机自己解析不了这个名字，所以宿主机探测要退回原地址。
    """
    raw_host = _host_of(raw)
    if raw_host in _LOCAL_HOST_ALIASES and _host_of(rewritten) != raw_host:
        return raw if "://" in raw else f"http://{raw}"
    return rewritten


# ---------------------------------------------------------------------------
# 宿主机视角探测（不走环境变量里的代理）
# ---------------------------------------------------------------------------


def _endpoint(url: str) -> tuple[str, int, str, str]:
    """URL → (host, port, scheme, path)。"""
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, port, scheme, path


def _probe_host(url: str, timeout: float) -> Probe:
    started = time.monotonic()

    def fail(detail: str, hint: str) -> Probe:
        return Probe("probe-host", "fail", detail, hint, time.monotonic() - started)

    host, port, scheme, path = _endpoint(url)
    if not host:
        return fail(f"URL 解析不出主机名：{url}", "写成 http://host:port/ 的形式")

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except socket.gaierror as exc:
        return fail(
            f"DNS 解析失败：{host}（{exc.strerror or exc}）",
            "确认域名拼写与 DNS；内网目标可在 /etc/hosts 固定解析",
        )
    except TimeoutError:
        # socket.timeout 在 3.10+ 就是 TimeoutError 的别名，写两个会被判重复捕获。
        return fail(
            f"TCP {host}:{port} 连接超时（>{timeout:g}s）",
            "目标未监听 / 被防火墙丢弃 / 需要跳板；先确认服务已启动",
        )
    except ConnectionRefusedError:
        return fail(
            f"TCP {host}:{port} 连接被拒绝",
            "主机在线但没有进程监听该端口；确认端口号或容器映射",
        )
    except OSError as exc:
        return fail(f"TCP {host}:{port} 失败：{exc}", "检查网络连通性与目标地址")

    tcp_elapsed = time.monotonic() - started
    if scheme not in ("http", "https"):
        return Probe(
            "probe-host", "ok", f"TCP {host}:{port} 已连通（{tcp_elapsed:.3f}s）", "", tcp_elapsed
        )

    status, headers = _http_request(scheme, host, port, path, timeout)
    if status is None:
        # TCP 连上不等于站点可用：透明代理 / TUN 模式下任意 IP 的 connect 都会
        # "秒成功"，真正能证明活着的只有 HTTP 响应。web 目标拿不到响应即判失败。
        return fail(
            f"TCP {host}:{port} 已连通（{tcp_elapsed:.3f}s），但 HTTP 无有效响应",
            "端口开着却没有 HTTP 应答：可能是非 HTTP 服务、被代理截获，或应用未就绪",
        )

    server = headers.get("server", "")
    detail = f"TCP {host}:{port} 已连通（{tcp_elapsed:.3f}s）· HTTP {status}"
    if server:
        detail += f" · server={server}"
    hint = ""
    if 500 <= status < 600:
        hint = f"服务返回 {status}：容器里的应用可能还没就绪（上次空跑就是 Caido 502）"
    elif 400 <= status < 500:
        hint = f"服务返回 {status}：目标在线但该路径不可用，确认路径/鉴权后再扫"
    return Probe("probe-host", "ok", detail, hint, time.monotonic() - started)


def _http_request(
    scheme: str, host: str, port: int, path: str, timeout: float
) -> tuple[int | None, dict[str, str]]:
    """发一次 GET，只取状态码与少量响应头；证书错也接受（靶场常自签）。"""
    factory = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    kwargs: dict[str, Any] = {"timeout": timeout}
    if scheme == "https":
        kwargs["context"] = ssl._create_unverified_context()  # noqa: S323 - 只看可达性
    conn = factory(host, port, **kwargs)
    try:
        conn.request("GET", path, headers={"User-Agent": "ben-preflight/1.0", "Accept": "*/*"})
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        resp.read(1024)
        return resp.status, headers
    except (OSError, http.client.HTTPException):
        return None, {}
    finally:
        conn.close()


def _probe_ip_ports(ip: str, ports: tuple[int, ...], timeout: float) -> tuple[Probe, list[int]]:
    """宿主机侧扫端口，返回（结论, 实际开放的端口）。"""
    started = time.monotonic()
    open_ports: list[int] = []
    errors: list[str] = []
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=min(timeout, 3.0)):
                open_ports.append(port)
        except (OSError, TimeoutError) as exc:
            errors.append(f"{port}:{type(exc).__name__}")
    elapsed = time.monotonic() - started
    if open_ports:
        joined = ", ".join(str(p) for p in open_ports)
        return Probe("probe-host", "ok", f"宿主机侧端口开放：{joined}", "", elapsed), open_ports
    joined = ", ".join(str(p) for p in ports)
    return (
        Probe(
            "probe-host",
            "fail",
            f"宿主机侧 {ip} 的 {joined} 全部不可达（{'; '.join(errors)}）",
            "用 --port 指定真实端口，或确认靶场已启动",
            elapsed,
        ),
        [],
    )


def _probe_repository(repo: str, timeout: float) -> Probe:
    started = time.monotonic()
    try:
        result = subprocess.run(  # noqa: S603 - 固定参数、无 shell
            ["git", "ls-remote", "--heads", repo],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return Probe("repo", "fail", "找不到 git 可执行文件", "安装 git 后重试")
    except subprocess.TimeoutExpired:
        return Probe(
            "repo", "fail", f"git ls-remote 超时（>{timeout:g}s）", "确认仓库地址与网络/凭据"
        )
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        return Probe(
            "repo",
            "fail",
            f"仓库不可访问：{message[0][:160] if message else 'git 返回非零'}",
            "确认地址、私有仓库凭据或 SSH key",
            elapsed,
        )
    heads = len([line for line in result.stdout.splitlines() if line.strip()])
    return Probe("repo", "ok", f"仓库可访问（{heads} 个分支）", "", elapsed)


# ---------------------------------------------------------------------------
# 沙箱视角探测（与扫描同一容器网络）
# ---------------------------------------------------------------------------


class _ContainerEngine:
    """懒创建一只一次性容器，专门用来从"扫描端"看目标。

    ``role`` 只影响容器名：预检与 recon 各起一只，互不抢占（同角色并发仍会
    撞名，它们的定位本来就是"同一时刻只跑一个"）。recon 复用本类而不是
    另写一份容器生命周期，是因为镜像拉取、``entrypoint=[]``（跳过 Caido）、
    网络选择这几处细节一旦出现第二份实现，迟早会与预检的口径不一致。
    """

    def __init__(self, settings: Any, *, pull: bool, role: str = "preflight") -> None:
        self.image = str(getattr(settings, "executor_image", "") or "")
        self.network = str(getattr(settings, "executor_network", "benptnet") or "benptnet")
        self.pull = pull
        self.role = role
        self._executor: Any | None = None
        self._broken = ""

    def prepare(self) -> Probe:
        try:
            from Ben.runtime.docker_connection import connect_docker

            client = connect_docker()
        except Exception as exc:  # noqa: BLE001 - 缺 SDK / 守护进程未起都只是"跳过容器探测"
            return Probe(
                "docker",
                "skip",
                f"Docker 不可用：{exc}",
                "无法验证容器视角；启动 Docker，或用 --no-docker 只做宿主机探测",
            )

        try:
            version = str(client.version().get("Version") or "?")
            if not client.images.list(name=self.image):
                if not self.pull:
                    return Probe(
                        "docker",
                        "skip",
                        f"镜像 {self.image} 本地不存在",
                        "先跑一次扫描（会自动拉取），或加 --pull 让预检拉取",
                    )
                repo, _, tag = self.image.partition(":")
                client.images.pull(repo, tag=tag or "latest")
        except Exception as exc:  # noqa: BLE001
            return Probe("docker", "fail", f"Docker 状态查询失败：{exc}", "检查 Docker 守护进程")
        finally:
            _close_quietly(client)

        try:
            from Ben.runtime.docker_executor import DockerExecutor

            self._executor = DockerExecutor(
                image=self.image,
                network=self.network,
                container_name=f"benpt-{self.role}-{os.getpid()}",
                entrypoint=[],  # 清空 entrypoint：跳过 Caido/代理初始化，直连看目标
            )
            self._executor.ensure_container()
        except Exception as exc:  # noqa: BLE001
            self._broken = str(exc)
            return Probe(
                "docker",
                "fail",
                f"预检容器启动失败：{exc}",
                "确认镜像可用、benpt-executor 未被并发占用（同一时刻只跑一个预检）",
            )
        return Probe("docker", "ok", f"Docker {version} · 网络 {self.network} · 镜像 {self.image}")

    def curl(self, url: str, timeout: float) -> Probe:
        started = time.monotonic()
        if self._executor is None:
            return Probe("probe-container", "skip", self._broken or "容器未就绪", "")

        timeout_s = f"{timeout:g}"
        command = [
            "curl",
            "-sS",
            "-o",
            "/dev/null",
            "-k",  # 靶场常自签证书，预检只判可达
            "--connect-timeout",
            timeout_s,
            "--max-time",
            timeout_s,
            "-w",
            _CURL_META + "%{http_code} %{time_connect} %{time_total} %{remote_ip}",
            "--",
            url,
        ]
        result = self._executor.run(command, timeout=timeout + 10)
        elapsed = time.monotonic() - started

        if result.timed_out or result.exit_code is None:
            return Probe(
                "probe-container",
                "fail",
                f"容器内 curl 超时（>{timeout:g}s 未返回）",
                "目标在容器网络里不可达：容器可能已退出，或不在同一 bridge 网络",
                elapsed,
            )

        meta = _last_meta(result.stdout)
        if result.exit_code != 0:
            reason = _CURL_ERRORS.get(result.exit_code, "curl 执行失败")
            detail = f"{reason}（curl exit={result.exit_code}）"
            stderr_tail = result.stderr.strip().splitlines()
            if stderr_tail:
                detail += f" · {stderr_tail[-1][:160]}"
            return Probe(
                "probe-container", "fail", detail, _hint_for_curl(result.exit_code, url), elapsed
            )

        parts = meta.split()
        status = parts[0] if parts else "?"
        connect_time = parts[1] if len(parts) > 1 else "?"
        remote_ip = parts[3] if len(parts) > 3 else "?"
        detail = f"容器内直连成功（{elapsed:.3f}s）· HTTP {status} · 对端 {remote_ip}"
        if connect_time not in ("?", ""):
            detail += f" · connect={connect_time}s"
        hint = ""
        if status.startswith("5"):
            hint = f"容器可达但返回 {status}：应用未就绪，扫描多半拿不到有效响应"
        return Probe("probe-container", "ok", detail, hint, elapsed)

    def curl_ports(self, ip: str, ports: tuple[int, ...], timeout: float) -> Probe:
        """IP 目标：逐端口试，任一端口通即算可达（避免只看第一个端口的误判）。"""
        started = time.monotonic()
        attempts: list[Probe] = []
        for port in ports:
            attempts.append(self.curl(f"http://{ip}:{port}/", timeout))
        elapsed = time.monotonic() - started
        ok = [a for a in attempts if a.ok]
        if ok:
            detail = "；".join(
                f"{port}:{_port_summary(a)}" for port, a in zip(ports, attempts, strict=False)
            )
            hints = next((a.hint for a in ok if a.hint), "")
            return Probe("probe-container", "ok", detail, hints, elapsed)
        if all(a.status == "skip" for a in attempts):
            return Probe(
                "probe-container", "skip", attempts[0].detail if attempts else "容器未就绪", ""
            )
        detail = "；".join(
            f"{port}:{_port_summary(a)}" for port, a in zip(ports, attempts, strict=False)
        )
        first_fail = next((a for a in attempts if a.status == "fail"), None)
        return Probe(
            "probe-container",
            "fail",
            detail,
            first_fail.hint if first_fail else "",
            elapsed,
        )

    @property
    def available(self) -> bool:
        """容器是否真的起来了（``prepare()`` 通过且执行器仍在）。"""
        return self._executor is not None

    @property
    def container_name(self) -> str:
        return f"benpt-{self.role}-{os.getpid()}"

    def execute(self, command: list[str], *, timeout: float) -> Any:
        """在容器里执行一条**已由调用方校验过**的命令，返回 CommandResult。

        本方法不做授权校验（``target_check`` 自己的探测也不需要），调用方
        ——目前是 ``recon``——必须先过 ``_check_command_allowed`` 再调这里。
        把两道事分开，是为了让"容器通道"保持成纯执行器，闸门只有一处。
        """
        if self._executor is None:
            raise RuntimeError(self._broken or "容器未就绪")
        return self._executor.run(command, timeout=timeout)

    def close(self) -> None:
        if self._executor is None:
            return
        try:
            self._executor.cleanup()
        except Exception:  # noqa: BLE001 - 清理失败不影响结论
            pass
        finally:
            self._executor = None


def _port_summary(probe: Probe) -> str:
    if probe.status == "skip":
        return "跳过"
    if probe.ok:
        code = ""
        if "HTTP " in probe.detail:
            code = probe.detail.split("HTTP ", 1)[1].split(" ·", 1)[0]
        return f"可达（{code}）" if code else "可达"
    return f"不可达（{probe.detail}）"


def _last_meta(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        if line.startswith(_CURL_META):
            return line[len(_CURL_META) :]
    return ""


def _close_quietly(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):  # 关不掉不影响结论
            close()


def _hint_for_curl(exit_code: int, url: str) -> str:
    host = _host_of(url) or "目标"
    if exit_code == 6:
        return f"容器内解析不到 {host}：给容器加 --add-host，或改用 IP / host.docker.internal"
    if exit_code == 7:
        return (
            f"容器内 {host} 拒绝连接：靶场容器可能已退出，或没加入同一 docker 网络；"
            "docker ps 确认后重试，必要时改成宿主机映射地址"
        )
    if exit_code == 28:
        return (
            f"容器内到 {host} 超时：目标不在 benptnet 可达网段，或被防火墙丢弃；"
            "确认靶场端口映射与容器网络后重试"
        )
    if exit_code in (35, 60):
        return "TLS 层失败：确认协议是 https 且服务端握手正常；纯 HTTP 端口不要写成 https"
    return "先在容器内手工复现：docker exec -it benpt-executor curl -v <target>"


# ---------------------------------------------------------------------------
# 单个目标的全套检查
# ---------------------------------------------------------------------------


def check_target(
    raw: str,
    *,
    allowed: tuple[str, ...],
    timeout: float,
    engine: _ContainerEngine | None,
    ports: tuple[int, ...] = (),
    container_skip_reason: str = "Docker 不可用",
) -> TargetReport:
    """分类 → 授权 → 宿主机探测 → 容器探测（+ 按类型补检查）。"""
    from Ben.interface.shared.targets import build_targets_info

    report = TargetReport(target=raw)

    try:
        infos = build_targets_info([raw])
    except ValueError as exc:
        message = str(exc).splitlines()[0]
        report.add(
            Probe("classify", "fail", message, "目标写法：URL / 域名 / IP / 本地目录 / git 仓库")
        )
        return report

    info = infos[0] if infos else {}
    report.kind = str(info.get("type") or "")
    details = info.get("details") or {}
    report.url = str(
        details.get("target_url")
        or details.get("target_repo")
        or details.get("target_ip")
        or details.get("target_path")
        or raw
    )

    handled_as = f"按 {report.kind} 处理"
    if report.url != raw:
        handled_as += f" · 容器内地址 {report.url}"
    report.add(Probe("classify", "ok", handled_as))

    # 只有网络目标才受 ALLOWED_TARGETS 约束：本地目录/仓库/spec 由宿主机处理，
    # 工具层的命令闸门也只在命令里出现主机时才生效。
    #
    # 注意：未授权不等于不可达。授权是"能不能扫"，可达是"通不通"，两件事分开
    # 结论 —— 否则用户想确认目标是否活着，只会拿到一句被白名单掩盖的"不可达"。
    if report.kind in ("web_application", "ip_address"):
        host = _host_of(raw) or _primary_host(info)
        authorized, auth_detail = _authorized(host, allowed)
        report.add(
            Probe(
                "authorize",
                "ok" if authorized else "fail",
                auth_detail,
                ""
                if authorized
                else "把该主机加进 .env 的 ALLOWED_TARGETS；否则工具层会逐条拒绝命令",
            )
        )
    else:
        report.add(Probe("authorize", "skip", f"{report.kind} 目标不走主机白名单，由宿主机侧处理"))

    if report.kind == "web_application":
        report.add(_probe_host(_host_view_url(raw, report.url), timeout))
        if engine is not None:
            report.add(engine.curl(report.url, timeout))
        else:
            report.add(Probe("probe-container", "skip", container_skip_reason, ""))
        report.add(_consistency_probe(report))
        return report

    if report.kind == "ip_address":
        ip = str(details.get("target_ip") or host)
        probe_ports = ports or _DEFAULT_IP_PORTS
        host_probe, open_ports = _probe_ip_ports(ip, probe_ports, timeout)
        report.add(host_probe)
        if engine is not None:
            report.add(engine.curl_ports(ip, tuple(open_ports) or probe_ports, timeout))
        else:
            report.add(Probe("probe-container", "skip", container_skip_reason, ""))
        report.add(_consistency_probe(report))
        return report

    if report.kind == "local_code":
        path = Path(str(details.get("target_path") or ""))
        readable = path.is_dir() and os.access(path, os.R_OK)
        report.add(
            Probe(
                "source",
                "ok" if readable else "fail",
                f"本地源码 {path} 可挂载进沙箱" if readable else f"{path} 不存在或不可读",
                "" if readable else "确认目录路径与权限",
            )
        )
        return report

    if report.kind == "repository":
        report.add(_probe_repository(str(details.get("target_repo") or ""), timeout))
        return report

    # api_spec：分类阶段已完成格式校验与本地存在性检查
    spec = str(details.get("target_spec") or details.get("source") or "")
    report.add(Probe("source", "ok", f"接口规格可用：{spec or raw}"))
    return report


def _consistency_probe(report: TargetReport) -> Probe:
    """宿主机可达但容器不可达，是最常见的"白跑"来源，单独点出来。"""
    host = report.find("probe-host")
    container = report.find("probe-container")
    if host is None or container is None or container.status == "skip":
        return Probe("consistency", "skip", "缺少容器视角样本，无法对比", "")
    if host.ok and not container.ok:
        return Probe(
            "consistency",
            "fail",
            "宿主机可达、容器内不可达：扫描端看不到这个服务",
            "靶场在宿主机就改用 http://host.docker.internal:<port>；"
            "在别的 docker 网络就 docker network connect <网络> benpt-executor",
        )
    if host.ok and container.ok:
        return Probe("consistency", "ok", "宿主机与容器两个视角都可达")
    return Probe("consistency", "skip", "宿主机侧未通过，跳过视角对比", "")


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

_STATUS_LABEL = {"ok": "OK", "fail": "FAIL", "skip": "SKIP"}


def _verdict(report: TargetReport) -> str:
    """单个目标的一句话结论：连通性与授权分开说。"""
    if report.fatal:
        return "目标无法识别"
    if not report.reachable:
        return "不可达"
    if not report.authorized:
        return "可达 · 未授权（通了也扫不了）"
    return "可达 · 已授权"


def _print_report(reports: list[TargetReport], allowed: tuple[str, ...], timeout: float) -> None:
    from Ben.interface.terminal.text import sanitize_terminal_text

    docker_probe = reports[0].find("docker") if reports else None
    docker_note = {"ok": "开启", "fail": "失败", "skip": "跳过"}.get(
        docker_probe.status if docker_probe else "", "关闭"
    )
    print(f"Ben 目标预检（timeout={timeout:g}s，容器视角={docker_note}）")
    print(f"授权范围：{', '.join(allowed) or '（空）'}")
    for report in reports:
        kind = f"  [{report.kind}]" if report.kind else ""
        print()
        print(f"目标  {sanitize_terminal_text(report.target)}{kind}")
        for probe in report.probes:
            if probe.name == "docker":
                continue
            label = _STATUS_LABEL.get(probe.status, probe.status.upper())
            print(f"  {probe.name:<18}{label:<6}{sanitize_terminal_text(probe.detail)}")
            if probe.hint:
                print(f"  {'':<18}{'':<6}↳ {sanitize_terminal_text(probe.hint)}")
        print(f"  {'结论':<16}{_verdict(report)}")

    unrecognized = [r for r in reports if r.fatal]
    down = [r for r in reports if not r.reachable and not r.fatal]
    unauthorized = [r for r in reports if r.reachable and not r.authorized]
    print()
    if unrecognized:
        print(f"结论 FAIL — {len(unrecognized)}/{len(reports)} 个目标无法识别：")
        for report in unrecognized:
            print(f"  · {sanitize_terminal_text(report.target)}")
        print("  目标写法：URL / 域名 / IP / 本地目录 / git 仓库")
    elif down:
        print(f"结论 FAIL — {len(down)}/{len(reports)} 个目标不可达：")
        for report in down:
            print(f"  · {sanitize_terminal_text(report.target)}")
        print("（确认无误仍要开跑：--skip-preflight；只想看宿主机视角：--no-docker）")
    elif unauthorized:
        print(
            f"结论 WARN — 目标都可达，但 {len(unauthorized)} 个不在授权范围内，工具层会拒绝命令："
        )
        for report in unauthorized:
            print(f"  · {sanitize_terminal_text(report.target)}")
        print("  把主机加进 .env 的 ALLOWED_TARGETS 后即可扫描；只想确认网络连通性则到此为止")
    else:
        print(f"结论 OK — {len(reports)}/{len(reports)} 个目标可达且已授权，可以启动扫描")


def _to_json(reports: list[TargetReport], allowed: tuple[str, ...], timeout: float) -> str:
    return json.dumps(
        {
            "allowed_targets": list(allowed),
            "timeout_seconds": timeout,
            "reachable": all(r.reachable for r in reports),
            "authorized": all(r.authorized for r in reports),
            "ok": all(r.ready for r in reports),
            "targets": [r.to_dict() for r in reports],
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ben preflight",
        description="检查目标网络是否可达、是否在授权范围内（只读，不产生 run、不调模型）",
        epilog=(
            "退出码：0 可达且已授权；1 不可达（网络/DNS/端口问题）；"
            "2 可达但未授权（加进 ALLOWED_TARGETS 即可）。"
            "不带目标时检查 .env 中 ALLOWED_TARGETS 的全部条目。"
        ),
    )
    parser.add_argument("targets", nargs="*", help="目标（URL/域名/IP/本地目录/git 仓库）")
    parser.add_argument(
        "-t",
        "--target",
        dest="extra_targets",
        action="append",
        default=[],
        metavar="TARGET",
        help="目标，可重复",
    )
    parser.add_argument(
        "--target-list",
        action="append",
        default=[],
        metavar="PATH",
        help="目标清单文件（每行一个，# 为注释）",
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, metavar="SECONDS", help="单次探测超时（默认 5 秒）"
    )
    parser.add_argument(
        "--port",
        type=int,
        action="append",
        default=[],
        metavar="PORT",
        help="IP 目标额外探测的端口，可重复（默认 80/443）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="只做宿主机探测，不启动容器（无法判断扫描端是否可达）",
    )
    parser.add_argument("--pull", action="store_true", help="镜像缺失时先拉取（默认跳过容器探测）")
    return parser


def _collect_targets(args: argparse.Namespace, allowed: tuple[str, ...]) -> list[str]:
    from Ben.interface.shared.targets import read_target_list_file

    targets = [t for t in args.targets if t]
    targets.extend(args.extra_targets or [])
    for path in args.target_list or []:
        targets.extend(read_target_list_file(path))
    seen: set[str] = set()
    ordered: list[str] = []
    for target in targets:
        if target not in seen:
            seen.add(target)
            ordered.append(target)
    return ordered or list(allowed)


def run_preflight(argv: list[str]) -> int:
    """``ben preflight`` 的实现：返回退出码，不抛业务异常。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")

    from Ben.config import load_settings

    try:
        settings = load_settings(require_api_key=False)
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要给出可读结论
        print(f"[FAIL] 读取配置失败：{exc}")
        return 1

    allowed = tuple(getattr(settings, "allowed_targets", ()) or ())
    try:
        targets = _collect_targets(args, allowed)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    if not targets:
        print("[FAIL] 没有可检查的目标：命令行未给目标，.env 里 ALLOWED_TARGETS 也是空的")
        print("       先设置 ALLOWED_TARGETS，再用 ben preflight <target> 检查")
        return 1

    engine: _ContainerEngine | None = None
    docker_probe: Probe | None = None
    if not args.no_docker:
        engine = _ContainerEngine(settings, pull=args.pull)
        docker_probe = engine.prepare()
        if docker_probe.status != "ok":
            engine = None  # 容器视角不可用，退回宿主机视角
    if args.no_docker:
        skip_reason = "已指定 --no-docker"
    elif docker_probe is not None and docker_probe.status != "ok":
        skip_reason = f"{docker_probe.detail}（只做了宿主机探测）"
    else:
        skip_reason = "容器视角不可用"

    reports: list[TargetReport] = []
    try:
        for target in targets:
            report = check_target(
                target,
                allowed=allowed,
                timeout=args.timeout,
                engine=engine,
                ports=tuple(args.port or ()),
                container_skip_reason=skip_reason,
            )
            if docker_probe is not None:
                report.probes.insert(0, docker_probe)
            reports.append(report)
    finally:
        if engine is not None:
            engine.close()

    if args.json:
        print(_to_json(reports, allowed, args.timeout))
    else:
        _print_report(reports, allowed, args.timeout)

    # 退出码把两种"不能开跑"分开，脚本可以据此决定补救动作：
    # 0 可达且已授权 / 1 不可达（网络问题） / 2 可达但未授权（改白名单即可）
    if not all(r.reachable for r in reports):
        return 1
    if not all(r.authorized for r in reports):
        return 2
    return 0


# ---------------------------------------------------------------------------
# 扫描前的自动闸门
# ---------------------------------------------------------------------------


def check_run_targets(args: argparse.Namespace) -> int | None:
    """扫描启动前的目标闸门：返回 None 表示放行，否则返回退出码。

    只对 web/ip 目标生效（本地目录、仓库、spec 由 prepare_run 自己校验）。
    """
    from Ben.config import load_settings

    settings = load_settings(require_api_key=False)
    allowed = tuple(getattr(settings, "allowed_targets", ()) or ())
    targets_info = list(getattr(args, "targets_info", None) or [])
    raw_targets = [
        str(info.get("original") or "")
        for info in targets_info
        if info.get("type") in ("web_application", "ip_address")
    ]
    raw_targets = [t for t in raw_targets if t]
    if not raw_targets:
        return None

    timeout = float(getattr(args, "preflight_timeout", 5.0) or 5.0)
    engine: _ContainerEngine | None = _ContainerEngine(settings, pull=False)
    probe = engine.prepare()
    skip_reason = "容器视角不可用"
    if probe.status != "ok":
        skip_reason = f"{probe.detail}（只做了宿主机探测）"
        engine = None

    reports: list[TargetReport] = []
    try:
        for target in raw_targets:
            report = check_target(
                target,
                allowed=allowed,
                timeout=timeout,
                engine=engine,
                container_skip_reason=skip_reason,
            )
            report.probes.insert(0, probe)
            reports.append(report)
    finally:
        if engine is not None:
            engine.close()

    _print_report(reports, allowed, timeout)
    if all(r.ready for r in reports):
        return None

    # 未授权一定拦：工具层会逐条拒绝命令，跑下去就是白烧额度。
    unauthorized = [r for r in reports if not r.authorized]
    if unauthorized:
        print()
        print(f"[FAIL] 目标不在授权范围（{len(unauthorized)} 个），已拒绝启动扫描")
        print("       没有创建 run，也没有消耗模型额度。把主机加进 .env 的 ALLOWED_TARGETS 后重试")
        for report in unauthorized:
            print(f"  · {report.target}")
        return 1

    # 只有"能下定论"的失败才拦：分类失败，或容器视角明确探测失败。
    # 容器视角不可用（Docker 异常）时宿主机视角的失败不足以拒绝启动 ——
    # 172.19.0.2 这类地址本来就只有容器能到。
    blocking = [r for r in reports if not r.reachable and _decisive(r)]
    if not blocking:
        print()
        print("[WARN] 容器视角不可用，无法判定扫描端是否可达；闸门放行，仅作提示")
        print("       先跑一次 --selftest-tools 确认执行器正常")
        return None

    print()
    print(f"[FAIL] 目标预检未通过（{len(blocking)} 个目标不可达），已拒绝启动扫描")
    print("       没有创建 run，也没有消耗模型额度。修好目标后重试；确认要跳过：--skip-preflight")
    return 1


def _decisive(report: TargetReport) -> bool:
    """这条结论能不能当拒绝启动的依据。"""
    if report.fatal:
        return True
    container = report.find("probe-container")
    return container is not None and container.status == "fail"
