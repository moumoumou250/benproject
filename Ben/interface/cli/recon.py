"""单目标信息收集（recon）：不调模型、不建 run、只发 GET/HEAD。

为什么单独做一个命令：

- ``preflight`` 只回答"通不通、授没授权"，不回答"这是什么"；
- 完整扫描要先过模型，一轮下来几十万 token，而人常常只想先看一眼这站是什么；
- sapper 的侦察兵是"花 4 分钟买一个判断"。本命令是它的确定性版本：同样的
  步骤、同样的顺序、同样的产物，但不烧额度、结果可 diff、可进 CI。

边界（刻意的）：

- 只发 GET（必要时 HEAD）；不提交表单、不登录、不爆破、不改任何东西；
- 每条容器命令都过 ``Ben.agents.factory._check_command_allowed`` —— 与 Agent
  完全同一道闸门，CLI 不比 Agent 多一分权限（这是本文件最重要的约束）；
- 目标必须在 ``ALLOWED_TARGETS`` 内，否则退出 2，一个请求都不发；
- 不写 ``strix_runs/``、不碰 run 记录：产物落在 ``recon/<slug>_<stamp>/``。

产物与 sapper 的 fact 同构（``kind`` + ``data``），便于被脚本或后续 run 读取：

    recon.json   结构化事实清单（host/service/http/tls/surface/endpoint/param/note）
    recon.md     人读版：清单 + 判定 + 建议下一步
    raw/         证据原文（只留文本类响应，单文件截断）

用法::

    ben recon http://127.0.0.1:3000/
    ben recon https://staging.example.com --json
    ben recon http://10.0.0.5:8080 --no-docker --max-paths 40
    ben recon http://host:3000 --path-list my-paths.txt --out /tmp/recon
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

_MAX_BODY_BYTES = 262_144
_MAX_RAW_BYTES = 65_536
_HDR_PATH = "/tmp/ben-recon.hdr"
_BODY_PATH = "/tmp/ben-recon.body"
_HDR_MARK = "__BEN_RECON_HEADERS__"
_BODY_MARK = "__BEN_RECON_BODY__"
_USER_AGENT = "ben-recon/1.0"
_DEFAULT_TIMEOUT = 10.0
_MAX_PATH_CHARS = 80

# 默认路径表：来自 sapper 的 kb001（指纹→目录→泄露→参数）与 Ben 自己的
# tooling/reconnaissance 片段，只保留"一个 GET 就能定性"的项。目录爆破不在这里，
# 那是扫描阶段按字典放大规模的事；这里只做定点确认。
_DEFAULT_PATHS: tuple[str, ...] = (
    "/robots.txt",
    "/sitemap.xml",
    "/.well-known/security.txt",
    "/.well-known/openid-configuration",
    "/.git/HEAD",
    "/.env",
    "/.DS_Store",
    "/backup.zip",
    "/admin",
    "/login",
    "/api",
    "/api/docs",
    "/swagger-ui/",
    "/openapi.json",
    "/v3/api-docs",
    "/graphql",
    "/actuator",
    "/actuator/health",
    "/console",
    "/server-status",
    "/phpinfo.php",
    "/debug",
    "/metrics",
    "/status",
)

_SECURITY_HEADERS: tuple[str, ...] = (
    "content-security-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
    "cross-origin-opener-policy",
)

# 指纹规则：(正则, 名称, 类别)。顺序即优先级，先命中的排前面。
_FINGERPRINTS: tuple[tuple[str, str, str], ...] = (
    (r"\bnginx/([\d.]+)", "nginx", "server"),
    (r"\bApache/([\d.]+)", "Apache", "server"),
    (r"\bMicrosoft-IIS/([\d.]+)", "IIS", "server"),
    (r"\bCaddy\b", "Caddy", "server"),
    (r"\benvoy\b", "Envoy", "server"),
    (r"\bTomcat/?([\d.]*)", "Tomcat", "server"),
    (r"\bJetty\b", "Jetty", "server"),
    (r"\bwerkzeug/?([\d.]*)", "Werkzeug", "server"),
    (r"\buvicorn\b", "Uvicorn", "server"),
    (r"\bgunicorn\b", "Gunicorn", "server"),
    (r"PHP/([\d.]+)", "PHP", "language"),
    (r"ASP\.NET", "ASP.NET", "framework"),
    (r"\bExpress\b", "Express", "framework"),
    # 带版本号的一律排在前面：同类命中只留第一条，先命中才留得住版本。
    (r"""content=["'][^"']*WordPress\s*([\d.]*)""", "WordPress", "cms"),
    (r"""content=["'][^"']*Drupal\s*([\d.]*)""", "Drupal", "cms"),
    (r"wp-content|wp-includes", "WordPress", "cms"),
    (r"Drupal\.settings|drupal.js", "Drupal", "cms"),
    (r"/media/jui/|joomla", "Joomla", "cms"),
    (r"__NEXT_DATA__", "Next.js", "framework"),
    (r"__NUXT__", "Nuxt", "framework"),
    (r"ng-version=\"([\d.]+)\"", "Angular", "frontend"),
    (r"data-reactroot|react\.production", "React", "frontend"),
    (r"csrfmiddlewaretoken", "Django", "framework"),
    (r"laravel_session|XSRF-TOKEN", "Laravel", "framework"),
    (r"swagger-ui|Swagger UI", "Swagger UI", "docs"),
    (r"graphiql|GraphQL", "GraphQL", "api"),
    (r"grafana", "Grafana", "product"),
    (r"jenkins", "Jenkins", "product"),
    (r"<generator>([^<]{1,60})</generator>", "generator", "meta"),
)

_CURL_ERRORS = {
    5: "代理解析失败",
    6: "DNS 解析失败",
    7: "TCP 连接被拒绝",
    28: "连接或读取超时",
    35: "TLS 握手失败",
    52: "服务端空回复",
    56: "接收数据失败",
    60: "证书校验失败",
}

_ASSET_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".css",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".map",
    ".mp4",
    ".pdf",
)

_TEXTUAL_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/xhtml",
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Response:
    """一次 GET 的结果。``error`` 非空表示没拿到 HTTP 响应。"""

    url: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    set_cookies: list[str] = field(default_factory=list)
    body: str = ""
    elapsed: float = 0.0
    error: str = ""
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400

    @property
    def content_type(self) -> str:
        return self.header("content-type")

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


@dataclass
class Fact:
    """一条结构化观察。``kind`` 对齐 sapper 的 fact 类型，便于两边互读。"""

    kind: str
    title: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "data": self.data,
            "source": self.source,
        }


class GateError(RuntimeError):
    """命令没过授权/只读闸门，或闸门本身不可用（失败即停，绝不静默放行）。"""


# ---------------------------------------------------------------------------
# 闸门与执行通道
# ---------------------------------------------------------------------------


def _gate(script: str, allowed: tuple[str, ...]) -> None:
    """把一条容器/本地命令交给 Agent 同款闸门校验。

    这里是"CLI 不比 Agent 多一分权限"的落点：不重写一套校验规则（那必然与
    factory 漂移），而是直接调用那一处唯一实现。闸门加载失败按失败处理——
    宁可不出结果，也不能在无校验的状态下把命令发出去。
    """
    try:
        from Ben.agents.factory import _check_command_allowed
    except Exception as exc:  # noqa: BLE001 - 缺依赖/导入期错误都要明确失败
        raise GateError(f"无法加载命令闸门（Ben.agents.factory）：{exc}") from exc
    reason = _check_command_allowed(script, allowed)
    if reason:
        raise GateError(reason)


def fetch_script(url: str, timeout: float) -> str:
    """构造取一个 URL 的 shell 片段。

    用两个 marker 把"头部"和"正文"分开：``curl -D -`` 会把 302→200 的每一跳都
    打到 stdout，正文里也可能有空行，靠 marker 定位才不会有歧义。``exit $BENRC``
    保住 curl 自己的退出码 —— 否则整段的退出码是 ``head`` 的 0，连不上也看不出来。
    """
    budget = f"{timeout:g}"
    return (
        f"curl -sS -k --compressed -D {_HDR_PATH} -o {_BODY_PATH} "
        f"--max-time {budget} --connect-timeout {budget} -A {_USER_AGENT} "
        f"-- {shlex.quote(url)} ; "
        "BENRC=$? ; "
        f"printf '\\n{_HDR_MARK}\\n' ; cat {_HDR_PATH} ; "
        f"printf '\\n{_BODY_MARK}\\n' ; head -c {_MAX_BODY_BYTES} {_BODY_PATH} ; "
        "exit $BENRC"
    )


def tls_script(host: str, port: int) -> str:
    """构造读证书的 openssl 命令。IP 目标不发 SNI（发了也只是个告警）。"""
    servername = "" if _looks_like_ip(host) else f" -servername {host}"
    return f"openssl s_client -connect {host}:{port}{servername} </dev/null 2>/dev/null"


def fetch_verbose_script(url: str, timeout: float) -> str:
    """没有 openssl 时的退路：``curl -v -I`` 的 verbose 输出里带证书主题/签发者。

    HEAD 仍属只读（GET/HEAD/OPTIONS 是项目允许的方法），闸门会照常校验主机。
    """
    budget = f"{timeout:g}"
    return (
        f"curl -sS -v -I -k --max-time {budget} --connect-timeout {budget} "
        f"-A {_USER_AGENT} -- {shlex.quote(url)}"
    )


class _Channel:
    """执行通道：优先容器内（与扫描同一视角），否则退回宿主机。"""

    def __init__(self, engine: Any | None, *, allowed: tuple[str, ...], timeout: float) -> None:
        self._engine = engine
        self._allowed = allowed
        self.timeout = timeout

    @property
    def label(self) -> str:
        if self._engine is not None and getattr(self._engine, "available", False):
            return f"容器 {self._engine.container_name}"
        return "宿主机（无容器视角，结论只代表本机网络位置）"

    @property
    def in_container(self) -> bool:
        return self._engine is not None and getattr(self._engine, "available", False)

    def shell(self, script: str, *, timeout: float | None = None) -> tuple[str, str, int | None]:
        """跑一段 shell 脚本，返回 (stdout, stderr, exit_code)。先过闸门。"""
        _gate(script, self._allowed)
        budget = (timeout if timeout is not None else self.timeout) + 10
        if self.in_container:
            result = self._engine.execute(["sh", "-c", script], timeout=budget)
            return result.stdout, result.stderr, result.exit_code
        try:
            proc = subprocess.run(  # noqa: S603 - 参数由本模块构造，无外部输入拼接
                ["sh", "-c", script],
                capture_output=True,
                text=True,
                timeout=budget,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "", f"本地命令执行失败：{exc}", None
        return proc.stdout, proc.stderr, proc.returncode

    def fetch(self, url: str) -> Response:
        """GET 一个 URL，头部与正文分开取（正文截断到 _MAX_BODY_BYTES）。"""
        started = time.monotonic()
        script = fetch_script(url, self.timeout)
        stdout, stderr, code = self.shell(script)
        elapsed = time.monotonic() - started
        if code is None:
            return Response(url=url, error=stderr.strip() or "命令未返回", elapsed=elapsed)
        if code != 0:
            detail = _CURL_ERRORS.get(code, "curl 执行失败")
            tail = stderr.strip().splitlines()
            message = f"{detail}（curl exit={code}）"
            if tail:
                message += f" · {tail[-1][:160]}"
            return Response(url=url, error=message, elapsed=elapsed)
        _, _, rest = stdout.partition(_HDR_MARK)
        header_text, _, body = rest.partition(_BODY_MARK)
        status, headers, cookies, redirects = _parse_headers(header_text)
        return Response(
            url=url,
            status=status,
            headers=headers,
            set_cookies=cookies,
            body=body.lstrip("\n"),
            elapsed=elapsed,
            truncated=len(body) >= _MAX_BODY_BYTES,
            redirects=redirects,
        )


def _parse_headers(block: str) -> tuple[int | None, dict[str, str], list[str], list[str]]:
    """解析 curl ``-D`` 输出；有多段（302 → 200）时以最后一段为准。"""
    status: int | None = None
    headers: dict[str, str] = {}
    cookies: list[str] = []
    redirects: list[str] = []
    for chunk in re.split(r"\r?\n\r?\n", block.strip()):
        lines = [line for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        match = re.match(r"HTTP/[\d.]+ (\d{3})", lines[0])
        if not match:
            continue
        # 新的一段开始：之前累积的是上一跳，重开一份。
        status = int(match.group(1))
        headers = {}
        cookies = []
        for line in lines[1:]:
            name, _, value = line.partition(":")
            key = name.strip().lower()
            val = value.strip()
            if not key or not val:
                continue
            if key == "set-cookie":
                cookies.append(val)
                continue
            headers[key] = f"{headers[key]}, {val}" if key in headers else val
        if "location" in headers:
            redirects.append(f"{status} → {headers['location']}")
    return status, headers, cookies, redirects


# ---------------------------------------------------------------------------
# 解析小工具
# ---------------------------------------------------------------------------


def _title_of(body: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    return text[:80]


def _is_textual(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(marker in lowered for marker in _TEXTUAL_TYPES)


def _body_signature(body: str) -> tuple[int, str]:
    """响应的"指纹"：长度 + 正文哈希。用来判断两条路径是否拿到同一个页面。"""
    return len(body), hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


def _is_same_page(response: Response, baseline: Response | None) -> bool:
    """这条响应是否就是"随便编一个路径"得到的那张兜底页。

    只认**逐字节相同**：状态码相同不算数（很多 404 也回 404 但带目录列表），
    长度相近也不算数（模板会插路径/时间戳）。宁可漏判，不可误判——误判的代价
    是把 /.git/HEAD、/.env 这种"高危结论"报给用户，比漏一条严重得多。
    """
    if baseline is None or baseline.error or response.error:
        return False
    if response.status != baseline.status:
        return False
    return _body_signature(response.body) == _body_signature(baseline.body)


def _fingerprint(headers: dict[str, str], body: str) -> list[dict[str, str]]:
    """从响应头与正文里认技术栈；认出来的是"线索"不是"结论"。"""
    haystack_parts = [
        headers.get("server", ""),
        headers.get("x-powered-by", ""),
        headers.get("via", ""),
        headers.get("x-generator", ""),
        body[:20000],
    ]
    haystack = "\n".join(part for part in haystack_parts if part)
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for pattern, name, category in _FINGERPRINTS:
        match = re.search(pattern, haystack, re.IGNORECASE)
        if not match or name in seen:
            continue
        seen.add(name)
        version = ""
        if match.groups():
            candidate = match.group(1)
            if candidate and re.fullmatch(r"[\d.]+", candidate):
                version = candidate
        found.append({"name": name, "version": version, "category": category})
    return found


def _cookie_flags(raw: str) -> dict[str, Any]:
    name = raw.split("=", 1)[0].strip()
    lowered = raw.lower()
    return {
        "name": name,
        "secure": "secure" in lowered,
        "httponly": "httponly" in lowered,
        "samesite": next(
            (
                part.split("=", 1)[1].strip()
                for part in raw.split(";")
                if part.strip().lower().startswith("samesite=")
            ),
            "",
        ),
    }


def _candidate_endpoints(text: str) -> list[str]:
    """从 HTML/JS 里捞候选路径。只捞引号包起来的、像路径的字符串。"""
    found: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"""["'`](/[A-Za-z0-9_\-./{}:?=&%]{2,120})["'`]""", text):
        path = match.group(1)
        if len(path) < 3 or path.lower().endswith(_ASSET_SUFFIXES):
            continue
        if path in seen:
            continue
        seen.add(path)
        found.append(path[:160])
        if len(found) >= 60:
            break
    return found


def _forms_of(body: str) -> list[dict[str, Any]]:
    forms: list[dict[str, Any]] = []
    for match in re.finditer(r"<form\b([^>]*)>(.*?)</form>", body, re.IGNORECASE | re.DOTALL):
        attrs = match.group(1)
        inner = match.group(2)
        action = re.search(r"""action=["']([^"']*)["']""", attrs, re.IGNORECASE)
        method = re.search(r"""method=["']([^"']*)["']""", attrs, re.IGNORECASE)
        fields = re.findall(r"""<input\b[^>]*name=["']([^"']+)["'][^>]*>""", inner, re.IGNORECASE)
        types = re.findall(r"""<input\b[^>]*type=["']([^"']+)["'][^>]*>""", inner, re.IGNORECASE)
        forms.append(
            {
                "action": (action.group(1) if action else "")[:160],
                "method": (method.group(1) if method else "get").upper(),
                "fields": fields[:20],
                "has_password": any(t.lower() == "password" for t in types),
            }
        )
        if len(forms) >= 20:
            break
    return forms


def _external_refs(body: str, host: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"""(?:src|href)=["'](https?://[^"']+)["']""", body, re.IGNORECASE):
        url = match.group(1)
        try:
            ref_host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if ref_host and ref_host != host and url not in seen:
            seen.add(url)
            refs.append(url[:160])
    return refs[:20]


def _script_srcs(body: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(r"""<script\b[^>]*src=["']([^"']+)["']""", body, re.IGNORECASE)
    ]


# ---------------------------------------------------------------------------
# 侦察本体
# ---------------------------------------------------------------------------


@dataclass
class ReconResult:
    target: str
    base_url: str
    host: str
    channel: str
    started_at: datetime
    facts: list[Fact] = field(default_factory=list)
    suggestions: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    def add(self, fact: Fact) -> None:
        self.facts.append(fact)

    def of_kind(self, kind: str) -> list[Fact]:
        return [f for f in self.facts if f.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for fact in self.facts:
            counts[fact.kind] = counts.get(fact.kind, 0) + 1
        return {
            "target": self.target,
            "url": self.base_url,
            "host": self.host,
            "channel": self.channel,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "counts": counts,
            "facts": [f.to_dict() for f in self.facts],
            "suggestions": self.suggestions,
            "notes": self.notes,
            "artifacts": self.artifacts,
        }


class Recon:
    """一次信息收集。所有网络动作都经 :class:`_Channel`，所有命令都过闸门。"""

    def __init__(
        self,
        *,
        target: str,
        base_url: str,
        host: str,
        allowed: tuple[str, ...],
        channel: _Channel,
        paths: tuple[str, ...],
        max_paths: int,
        max_js: int,
        frontend: bool,
        raw_dir: Path | None,
    ) -> None:
        self.target = target
        self.base_url = base_url
        self.host = host
        self.allowed = allowed
        self.channel = channel
        self.paths = paths
        self.max_paths = max_paths
        self.max_js = max_js
        self.frontend = frontend
        self.raw_dir = raw_dir
        self.result = ReconResult(
            target=target,
            base_url=base_url,
            host=host,
            channel=channel.label,
            started_at=datetime.now(UTC),
        )
        self._root: Response | None = None
        self._raw_count = 0

    # -- 主流程 ------------------------------------------------------------

    def run(self) -> ReconResult:
        self._step_dns()
        self._step_root()
        self._step_tls()
        self._step_paths()
        if self.frontend and self._root is not None and self._root.ok:
            self._step_frontend()
        self.result.suggestions = _suggestions(self.result)
        return self.result

    # -- 各步骤 ------------------------------------------------------------

    def _step_dns(self) -> None:
        """DNS 只看本机解析结果；容器内解析可能不同，所以标注来源。"""
        addresses: list[dict[str, str]] = []
        try:
            infos = socket.getaddrinfo(self.host, None, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            if self.channel.in_container:
                # 容器网络里的服务名（juice-shop 之类）宿主机本来就不认识，
                # 这不是失败，只是"这个解析器不在同一个 DNS 里"。
                self.result.notes.append(
                    f"宿主机解析不到 {self.host}（该名字只在 docker 网络内有效）；"
                    "容器视角的结论不受影响"
                )
            else:
                self.result.notes.append(f"DNS 解析失败：{exc}")
            return
        seen: set[str] = set()
        for info in infos:
            address = str(info[4][0])
            if address in seen:
                continue
            seen.add(address)
            family = "ipv6" if info[0] == socket.AF_INET6 else "ipv4"
            addresses.append({"address": address, "family": family})
        self.result.add(
            Fact(
                "host",
                self.host,
                {"hostname": self.host, "addresses": addresses, "resolver": "ben-host"},
                "dns",
            )
        )

    def _step_root(self) -> None:
        response = self.channel.fetch(self.base_url)
        self._root = response
        if response.error:
            self.result.notes.append(f"根路径请求失败：{response.error}")
            if self.channel.in_container and "host.docker.internal" in self.base_url:
                self.result.notes.append(
                    "容器内解析不到 host.docker.internal：改用 --no-docker，"
                    "或把目标换成容器网络内的地址"
                )
            return
        scheme = urlsplit(self.base_url).scheme.lower()
        self.result.add(
            Fact(
                "http",
                "根路径响应",
                {
                    "url": self.base_url,
                    "status": response.status,
                    "server": response.header("server"),
                    "powered_by": response.header("x-powered-by"),
                    "content_type": response.content_type,
                    "bytes": len(response.body),
                    "elapsed_ms": round(response.elapsed * 1000, 1),
                    "redirects": response.redirects,
                    "title": _title_of(response.body),
                },
                "http-root",
            )
        )
        present = [h for h in _SECURITY_HEADERS if response.header(h)]
        missing = [h for h in _SECURITY_HEADERS if not response.header(h)]
        self.result.add(
            Fact(
                "http",
                "安全响应头",
                {"present": present, "missing": missing},
                "http-root",
            )
        )
        tech = _fingerprint(response.headers, response.body)
        if tech:
            self.result.add(Fact("service", "技术栈指纹", {"items": tech}, "fingerprint"))
        if response.set_cookies:
            self.result.add(
                Fact(
                    "service",
                    "Cookie 属性",
                    {"cookies": [_cookie_flags(raw) for raw in response.set_cookies[:20]]},
                    "http-root",
                )
            )
        if "https" in scheme and response.header("strict-transport-security") == "":
            self.result.notes.append("https 站点未下发 HSTS：仅作加固建议，不认定为漏洞")
        self._save_raw("root", response)

    def _step_tls(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme.lower() != "https":
            self.result.add(Fact("tls", "未启用 TLS", {"scheme": parsed.scheme or "http"}, "tls"))
            return
        host = parsed.hostname or self.host
        port = parsed.port or 443
        stdout, stderr, code = self.channel.shell(
            tls_script(host, port), timeout=self.channel.timeout
        )
        if code == 0 and "subject=" in stdout:
            info = _parse_cert(stdout)
            info.update(
                {"host": f"{host}:{port}", "protocol": _find(r"Protocol\s*:\s*(\S+)", stdout)}
            )
            self.result.add(Fact("tls", "证书与协议", info, "tls"))
            return
        # 执行镜像里没有 openssl（strix-sandbox 就只带了 curl）。退一步用 curl -v：
        # 拿得到 subject/issuer/到期时间，拿不到 SAN —— 明说缺了什么，别假装完整。
        _, verbose_text, verbose_code = self.channel.shell(
            fetch_verbose_script(self.base_url, self.channel.timeout),
            timeout=self.channel.timeout,
        )
        if verbose_code == 0 and "subject:" in verbose_text:
            info = _parse_cert_verbose(verbose_text)
            info.update({"host": f"{host}:{port}", "source": "curl -v"})
            self.result.add(Fact("tls", "证书与协议", info, "tls-degraded"))
            self.result.notes.append(
                "容器内没有 openssl：证书主题/签发者取自 curl -v，SAN 与剩余天数未采集；"
                "需要完整证书信息时用 --no-docker"
            )
            return
        self.result.notes.append(
            f"TLS 证书读取失败（openssl exit={code}）：{(stderr or '').strip()[:160] or '无输出'}"
        )

    def _step_paths(self) -> None:
        limit = min(len(self.paths), self.max_paths)
        robots_body = ""
        baseline = self._probe_baseline()
        for path in self.paths[:limit]:
            url = urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))
            response = self.channel.fetch(url)
            if response.error:
                self.result.add(
                    Fact(
                        "surface",
                        path,
                        {"url": url, "status": None, "error": response.error, "fallback": False},
                        "paths",
                    )
                )
                continue
            fallback = _is_same_page(response, baseline)
            self.result.add(
                Fact(
                    "surface",
                    path,
                    {
                        "url": url,
                        "status": response.status,
                        "bytes": len(response.body),
                        "content_type": response.content_type,
                        "location": response.header("location"),
                        "title": _title_of(response.body),
                        "fallback": fallback,
                    },
                    "paths",
                )
            )
            if fallback:
                # 软 404 的正文就是同一张首页：存下来只会让 raw/ 里躺着 20 份
                # 一模一样的东西，还会被后来的人当成"我拿到了 .env"。
                continue
            if response.ok and _is_textual(response.content_type):
                self._save_raw(path, response)
            if path == "/robots.txt" and response.ok:
                robots_body = response.body
        # robots.txt 里的 Disallow 是免费的目录线索：一次请求已经拿到，直接解析。
        disallow = [
            line.split(":", 1)[1].strip()
            for line in robots_body.splitlines()
            if line.strip().lower().startswith("disallow:")
        ]
        disallow = [item for item in disallow if item and item != "/"][:40]
        if disallow:
            self.result.add(
                Fact("surface", "robots.txt 指向的路径", {"disallow": disallow}, "robots")
            )

    def _probe_baseline(self) -> Response | None:
        """先探一个几乎不可能存在的随机路径，作为"软 404"基线。

        SPA（React/Vue 前台、Juice Shop 这类）会把任何未知路径都渲染成首页并
        回 200。没有基线，/.git/HEAD、/.env、/backup.zip 全会因为"200"被误判
        成泄露——这是最伤信任的一类假阳性：结论越吓人，越要先证明它是真的。
        基线本身也留成一条事实（``baseline: true``），让读 JSON 的人能复核。
        """
        token = f"/ben-missing-{os.urandom(6).hex()}"
        url = urljoin(self.base_url.rstrip("/") + "/", token.lstrip("/"))
        response = self.channel.fetch(url)
        if response.error:
            self.result.notes.append(
                f"软 404 基线探测失败：{response.error}（路径结论未经基线校验）"
            )
            return None
        self.result.add(
            Fact(
                "surface",
                token,
                {
                    "url": url,
                    "status": response.status,
                    "bytes": len(response.body),
                    "content_type": response.content_type,
                    "title": _title_of(response.body),
                    "baseline": True,
                    "fallback": False,
                },
                "baseline",
            )
        )
        return response

    def _step_frontend(self) -> None:
        root = self._root
        assert root is not None  # noqa: S101 - run() 已保证
        endpoints = _candidate_endpoints(root.body)
        forms = _forms_of(root.body)
        scripts = _script_srcs(root.body)
        same_host_scripts: list[str] = []
        off_scope: list[str] = []
        for src in scripts:
            absolute = urljoin(root.url, src)
            ref_host = (urlsplit(absolute).hostname or "").lower()
            if ref_host and ref_host != self.host:
                off_scope.append(absolute[:160])
                continue
            same_host_scripts.append(absolute)
        for script_url in same_host_scripts[: self.max_js]:
            response = self.channel.fetch(script_url)
            if response.error or not response.ok:
                continue
            endpoints.extend(_candidate_endpoints(response.body))
            self._save_raw(f"scripts/{Path(urlsplit(script_url).path).name}", response)
        unique: list[str] = []
        seen: set[str] = set()
        for item in endpoints:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        if unique:
            self.result.add(
                Fact(
                    "endpoint",
                    "候选接口/路径",
                    {"items": unique[:60], "count": len(unique)},
                    "frontend",
                )
            )
        if forms:
            self.result.add(Fact("param", "页面表单", {"forms": forms}, "frontend"))
        if off_scope:
            self.result.add(
                Fact(
                    "note",
                    "跨主机引用（未抓取）",
                    {"urls": off_scope, "reason": "不在授权主机范围内，闸门会拒绝"},
                    "frontend",
                )
            )
        self.result.add(
            Fact(
                "service",
                "前端构成",
                {"scripts": len(scripts), "same_host_scripts": len(same_host_scripts)},
                "frontend",
            )
        )

    # -- 证据落盘 ------------------------------------------------------------

    def _save_raw(self, name: str, response: Response) -> None:
        """把文本响应原文存成证据。只留能读的，二进制不落盘。"""
        if self.raw_dir is None or not _is_textual(response.content_type):
            return
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip("/"))[:60] or "response"
        if self._raw_count >= 60:
            return
        target = self.raw_dir / f"{safe}.txt"
        index = 1
        while target.exists():
            target = self.raw_dir / f"{safe}-{index}.txt"
            index += 1
        body = response.body[:_MAX_RAW_BYTES]
        header_lines = "".join(f"{k}: {v}\n" for k, v in sorted(response.headers.items()))
        target.write_text(
            f"# {response.url}\n# status: {response.status}\n{header_lines}\n{body}",
            encoding="utf-8",
        )
        self._raw_count += 1


def _looks_like_ip(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value)) or ":" in value


def _find(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _days_left(not_after: str) -> int | None:
    if not not_after:
        return None
    try:
        expiry = parsedate_to_datetime(" ".join(not_after.split()))
    except (TypeError, ValueError):
        return None
    return (expiry - datetime.now(UTC)).days


def _parse_cert(text: str) -> dict[str, Any]:
    san_block = ""
    match = re.search(r"X509v3 Subject Alternative Name:\s*\n\s*(.+)", text, re.IGNORECASE)
    if match:
        san_block = match.group(1)
    sans = [item.replace("DNS:", "").strip() for item in san_block.split(",") if item.strip()]
    not_after = _find(r"Not After\s*:\s*(.+)", text)
    return {
        "subject": _find(r"subject=\s*(.+)", text)[:200],
        "issuer": _find(r"issuer=\s*(.+)", text)[:200],
        "sans": sans[:40],
        "not_after": not_after,
        "days_left": _days_left(not_after),
    }


def _parse_cert_verbose(text: str) -> dict[str, Any]:
    """解析 ``curl -v`` 的证书行：主题/签发者/到期/协议，SAN 拿不到。"""

    def grab(pattern: str) -> str:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else ""

    not_after = grab(r"^\*\s+expire date:\s*(.+)$")
    return {
        "subject": grab(r"^\*\s+subject:\s*(.+)$")[:200],
        "issuer": grab(r"^\*\s+issuer:\s*(.+)$")[:200],
        "sans": [],
        "not_after": not_after,
        "days_left": _days_left(not_after),
        "protocol": grab(r"SSL connection using (\S+)"),
    }


# ---------------------------------------------------------------------------
# 建议：把事实翻译成"下一步做什么"
# ---------------------------------------------------------------------------


def _status_of(result: ReconResult, path: str) -> int | None:
    for fact in result.of_kind("surface"):
        # 软 404 的 200 不是"这条路径存在"，它对任何路径都回 200；拿它当证据
        # 会产出"源码仓库暴露"这种一戳就破的结论。
        if fact.data.get("fallback") or fact.data.get("baseline"):
            continue
        if fact.title == path:
            status = fact.data.get("status")
            return status if isinstance(status, int) else None
    return None


def _suggestions(result: ReconResult) -> list[dict[str, str]]:
    """确定性规则，不做推理。每条都给"为什么"与"交给谁"。"""
    out: list[dict[str, str]] = []

    def add(title: str, why: str, nxt: str) -> None:
        out.append({"title": title, "why": why, "next": nxt})

    baseline = next((f for f in result.of_kind("surface") if f.data.get("baseline")), None)
    fallbacks = [f for f in result.of_kind("surface") if f.data.get("fallback")]
    if baseline and baseline.data.get("status") == 200:
        add(
            "目标对未知路径统一回首页（软 404）",
            f"随机路径 {baseline.title} 返回 200/{baseline.data.get('bytes', 0)} B，"
            f"{len(fallbacks)} 条探测结果与之逐字节相同",
            "路径探测只能拿来做否定证据：改用接口枚举（/api/…）与已确认路由，别据此判泄露",
        )

    if _status_of(result, "/.git/HEAD") == 200:
        add("源码仓库暴露", "/.git/HEAD 返回 200", "按 git 泄露流程恢复源码与历史凭据")
    for path in ("/.env", "/.DS_Store", "/backup.zip"):
        if _status_of(result, path) == 200:
            add("敏感文件可直接下载", f"{path} 返回 200", "先取回原文核对内容，再判断是否含凭据")
    for path in ("/swagger-ui/", "/openapi.json", "/v3/api-docs", "/api/docs"):
        if _status_of(result, path) == 200:
            add(
                "接口清单可用", f"{path} 返回 200", "把该地址作为 api_spec 目标重跑，转到参数级测试"
            )
            break
    for path in (
        "/actuator",
        "/actuator/health",
        "/console",
        "/server-status",
        "/phpinfo.php",
        "/metrics",
        "/debug",
    ):
        if _status_of(result, path) == 200:
            add(
                "管理/调试端点暴露",
                f"{path} 返回 200",
                "交给 information_disclosure，先确认是否需认证",
            )
            break
    if _status_of(result, "/graphql") == 200:
        add(
            "GraphQL 端点在线",
            "/graphql 返回 200",
            "先取 schema（introspection），再查越权与批量查询",
        )
    for path in ("/admin", "/login"):
        status = _status_of(result, path)
        if status == 200:
            add("登录面存在", f"{path} 返回 200", "认证面优先：弱口令、认证绕过、会话固定")
        elif status == 403:
            add("受限端点存在", f"{path} 返回 403", "说明后端有该路由；拿到低权限账号后复测越权")

    tech = next((f for f in result.of_kind("service") if f.title == "技术栈指纹"), None)
    if tech:
        with_version = [
            i["name"] + "/" + i["version"] for i in tech.data.get("items", []) if i.get("version")
        ]
        if with_version:
            add("版本指纹已拿到", "、".join(with_version[:5]), "先查该版本已公开 CVE，再谈利用路径")

    forms = next((f for f in result.of_kind("param") if f.title == "页面表单"), None)
    if forms and any(item.get("has_password") for item in forms.data.get("forms", [])):
        add(
            "存在带密码字段的表单", "根页面含 password 输入框", "记录字段名、错误信息差异与锁定策略"
        )

    headers_fact = next((f for f in result.of_kind("http") if f.title == "安全响应头"), None)
    if headers_fact and headers_fact.data.get("missing"):
        add(
            "安全响应头缺失",
            "缺失：" + "、".join(headers_fact.data["missing"][:4]),
            "只作加固建议；按项目口径不认定为漏洞",
        )

    if result.of_kind("endpoint"):
        count = result.of_kind("endpoint")[0].data.get("count", 0)
        add("前端暴露候选接口", f"共 {count} 条候选路径", "挑带参数/带 id 的接口做越权与注入验证")
    return out[:8]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _is_interesting(status: Any) -> bool:
    """人读版只列"说明有问题"的状态：可达、需认证、出错，或根本没响应。"""
    if status is None:
        return True
    if not isinstance(status, int):
        return True
    return status < 400 or status in (401, 403) or status >= 500


def _verdict_line(result: ReconResult) -> str:
    tech = next((f for f in result.of_kind("service") if f.title == "技术栈指纹"), None)
    names = "、".join(
        item["name"] + (f"/{item['version']}" if item.get("version") else "")
        for item in (tech.data.get("items", []) if tech else [])[:4]
    )
    reachable = sum(
        1
        for f in result.of_kind("surface")
        if not f.data.get("fallback")
        and not f.data.get("baseline")
        and isinstance(f.data.get("status"), int)
        and 200 <= f.data["status"] < 400
    )
    parts = [f"可达路径 {reachable} 条"]
    fallbacks = sum(1 for f in result.of_kind("surface") if f.data.get("fallback"))
    if fallbacks:
        parts.append(f"软 404 {fallbacks} 条已排除")
    if names:
        parts.append(f"指纹 {names}")
    parts.append(f"建议 {len(result.suggestions)} 条")
    return " · ".join(parts)


def _print_human(result: ReconResult) -> None:
    from Ben.interface.terminal.text import sanitize_terminal_text as clean

    print(f"Ben 信息收集（recon）— {clean(result.base_url)}")
    print(f"通道：{clean(result.channel)}")
    print(f"开始：{result.started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    hosts = result.of_kind("host")
    if hosts:
        addresses = ", ".join(item["address"] for item in hosts[0].data.get("addresses", [])[:6])
        print(f"主机   {clean(result.host)} → {clean(addresses or '未解析')}")
    root = next((f for f in result.of_kind("http") if f.title == "根路径响应"), None)
    if root:
        data = root.data
        line = f"服务   HTTP {data['status']} · {data.get('server') or '无 Server 头'}"
        if data.get("title"):
            line += f" · {data['title']}"
        print(clean(line))
    tls = result.of_kind("tls")
    if tls:
        data = tls[0].data
        if data.get("sans"):
            days = data.get("days_left")
            tail = f" · 剩余 {days} 天" if isinstance(days, int) else ""
            print(clean(f"证书   {data.get('issuer', '')}{tail}"))
        elif data.get("scheme"):
            print(clean(f"证书   未启用（{data['scheme']}）"))

    headers_fact = next((f for f in result.of_kind("http") if f.title == "安全响应头"), None)
    if headers_fact and headers_fact.data.get("missing"):
        missing = headers_fact.data["missing"]
        shown = "、".join(missing[:4])
        more = f" 等 {len(missing)} 项" if len(missing) > 4 else ""
        print(clean(f"响应头 缺失 {shown}{more}"))

    surfaces = [
        f for f in result.of_kind("surface") if f.source == "paths" and not f.data.get("fallback")
    ]
    if surfaces:
        print()
        print("暴露面")
        interesting = [f for f in surfaces if _is_interesting(f.data.get("status"))]
        for fact in interesting:
            status = fact.data.get("status")
            marker = "·" if status is None else str(status)
            size = fact.data.get("bytes", 0)
            title = fact.data.get("title", "") or fact.data.get("error", "")
            print(f"  {marker:<4} {fact.title:<34} {size:>7} B  {clean(str(title))[:60]}")
        # 404 只是"这条路径没有"，逐条打印会把真正的暴露面淹掉；只留一行合计。
        quiet = len(surfaces) - len(interesting)
        if quiet:
            print(clean(f"  其余 {quiet} 条返回 404/无响应（明细见 recon.json）"))
    fallbacks = [f for f in result.of_kind("surface") if f.data.get("fallback")]
    if fallbacks:
        names = "、".join(f.title for f in fallbacks[:6])
        more = f" 等 {len(fallbacks)} 条" if len(fallbacks) > 6 else ""
        print(clean(f"  软 404  {names}{more} 全是同一张兜底页，未计入暴露面"))
    robots = next(
        (f for f in result.of_kind("surface") if f.title == "robots.txt 指向的路径"), None
    )
    if robots:
        print(clean("  robots: " + "、".join(robots.data["disallow"][:12])))

    frontend = next((f for f in result.of_kind("service") if f.title == "前端构成"), None)
    endpoints = result.of_kind("endpoint")
    if frontend or endpoints:
        print()
        parts = []
        if frontend:
            parts.append(f"脚本 {frontend.data.get('scripts', 0)} 个")
        if endpoints:
            parts.append(f"候选接口 {endpoints[0].data.get('count', 0)} 条")
        forms = next((f for f in result.of_kind("param") if f.title == "页面表单"), None)
        if forms:
            parts.append(f"表单 {len(forms.data.get('forms', []))} 个")
        print(clean("前端   " + " · ".join(parts)))

    if result.suggestions:
        print()
        print("建议")
        for item in result.suggestions:
            print(clean(f"  · {item['title']}（{item['why']}）→ {item['next']}"))
    for note in result.notes:
        print()
        print(clean(f"注意   {note}"))
    if result.artifacts:
        print()
        print(clean(f"产物   {result.artifacts.get('dir', '')}"))
        print(f"  {_verdict_line(result)}")


def _render_markdown(result: ReconResult) -> str:
    lines = [
        f"# 信息收集 — {result.base_url}",
        "",
        f"- 目标：`{result.target}`",
        f"- 主机：`{result.host}`",
        f"- 通道：{result.channel}",
        f"- 时间：{result.started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 结论：{_verdict_line(result)}",
        "",
        "> 只读侦察：仅 GET；未提交表单、未登录、未爆破。安全响应头缺失按项目口径仅作加固建议。",
        "",
        "## 事实清单",
        "",
    ]
    for fact in result.facts:
        if fact.data.get("fallback"):
            # 软 404 逐条展开会占满整份报告；下面合并成一段。
            continue
        lines.append(f"### {fact.kind} · {fact.title}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(fact.data, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    fallbacks = [f for f in result.of_kind("surface") if f.data.get("fallback")]
    if fallbacks:
        lines.append("## 软 404（已从暴露面排除）")
        lines.append("")
        lines.append("下列路径的响应与随机路径基线**逐字节相同**，状态码 200 不代表路径存在：")
        lines.append("")
        for fact in fallbacks:
            lines.append(
                f"- `{fact.title}` — {fact.data.get('status')} / {fact.data.get('bytes')} B"
            )
        lines.append("")
    if result.suggestions:
        lines.append("## 建议下一步")
        lines.append("")
        for item in result.suggestions:
            lines.append(f"- **{item['title']}** — {item['why']}；{item['next']}")
        lines.append("")
    if result.notes:
        lines.append("## 注意")
        lines.append("")
        for note in result.notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines)


def _write_artifacts(result: ReconResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "recon.json"
    md_path = out_dir / "recon.md"
    json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(_render_markdown(result), encoding="utf-8")
    result.artifacts = {
        "dir": str(out_dir),
        "json": str(json_path),
        "markdown": str(md_path),
        "raw": str(out_dir / "raw") if (out_dir / "raw").is_dir() else "",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ben recon",
        description="对单个授权站点做只读信息收集（不调模型、不建 run、产物可 diff）",
        epilog=(
            "退出码：0 完成；1 目标不可达/执行失败；2 目标不在 ALLOWED_TARGETS 内（一个请求都没发）。"
            "只发 GET：不提交表单、不登录、不爆破。"
        ),
    )
    parser.add_argument("target", help="目标 URL（必须已写进 .env 的 ALLOWED_TARGETS）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="单请求超时（默认 10 秒）",
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON（便于管道处理）")
    parser.add_argument("--out", metavar="DIR", help="产物目录（默认 recon/<host>_<时间戳>/）")
    parser.add_argument("--no-write", action="store_true", help="不落盘，只打印结论")
    parser.add_argument("--no-paths", action="store_true", help="跳过默认路径表探测")
    parser.add_argument("--path-list", metavar="FILE", help="自定义路径清单（每行一个，# 为注释）")
    parser.add_argument(
        "--max-paths",
        type=int,
        default=len(_DEFAULT_PATHS),
        metavar="N",
        help=f"最多探测多少条路径（默认 {len(_DEFAULT_PATHS)}）",
    )
    parser.add_argument("--no-frontend", action="store_true", help="跳过 HTML/JS 提取（不抓脚本）")
    parser.add_argument(
        "--max-js", type=int, default=3, metavar="N", help="最多抓几个同源脚本（默认 3）"
    )
    parser.add_argument("--no-docker", action="store_true", help="只在宿主机侧收集，不启动容器")
    parser.add_argument("--pull", action="store_true", help="镜像缺失时先拉取")
    return parser


def _load_paths(args: argparse.Namespace) -> tuple[str, ...]:
    if args.no_paths:
        return ()
    if not args.path_list:
        return _DEFAULT_PATHS
    path = Path(args.path_list)
    if not path.is_file():
        raise ValueError(f"路径清单不存在：{path}")
    items: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(stripped if stripped.startswith("/") else f"/{stripped}")
    if not items:
        raise ValueError(f"路径清单为空：{path}")
    return tuple(items)


def _slug(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("_") or "target"


def run_recon(argv: list[str]) -> int:
    """``ben recon`` 的实现：返回退出码，不抛业务异常。

    顺序是刻意的：分类（不联网）→ 授权（不联网）→ 建容器 → 预检 → 收集。
    授权不过就在**发出第一个请求之前**退出，这样"未授权"是真的零网络动作，
    而不是"探测了一下再说不行"。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.max_paths < 0 or args.max_js < 0:
        parser.error("--max-paths / --max-js 不能为负")

    from Ben.config import load_settings

    try:
        settings = load_settings(require_api_key=False)
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要给出可读结论
        print(f"[FAIL] 读取配置失败：{exc}")
        return 1
    allowed = tuple(getattr(settings, "allowed_targets", ()) or ())

    try:
        paths = _load_paths(args)
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1

    from Ben.interface.cli.target_check import _authorized, _ContainerEngine, _host_of, check_target
    from Ben.interface.shared.targets import build_targets_info

    try:
        infos = build_targets_info([args.target])
    except ValueError as exc:
        print(f"[FAIL] 目标无法识别：{str(exc).splitlines()[0]}")
        return 1
    kind = str((infos[0] if infos else {}).get("type") or "")
    if kind not in ("web_application", "ip_address"):
        print(f"[FAIL] recon 只支持 web/ip 目标，当前是 {kind or '未知'}")
        return 1

    host = _host_of(args.target) or str((infos[0].get("details") or {}).get("target_ip") or "")
    authorized, auth_detail = _authorized(host, allowed)
    if not authorized:
        print(f"[FAIL] {args.target} 不在授权范围，未发出任何请求")
        print(f"       {auth_detail}")
        print("       把它加进 .env 的 ALLOWED_TARGETS 后重试")
        return 2

    engine: _ContainerEngine | None = None
    docker_probe = None
    if not args.no_docker:
        engine = _ContainerEngine(settings, pull=args.pull, role="recon")
        docker_probe = engine.prepare()
        if docker_probe.status != "ok":
            print(f"[WARN] 容器视角不可用：{docker_probe.detail}")
            print("       退回宿主机视角；结论只代表本机网络位置，不等于扫描端可达")
            engine = None

    try:
        report = check_target(
            args.target,
            allowed=allowed,
            timeout=args.timeout,
            engine=engine,
            container_skip_reason="已指定 --no-docker" if args.no_docker else "容器视角不可用",
        )
        if docker_probe is not None:
            report.probes.insert(0, docker_probe)
        return _recon_with_report(args, report=report, engine=engine, allowed=allowed, paths=paths)
    finally:
        if engine is not None:
            engine.close()


def _recon_with_report(
    args: argparse.Namespace,
    *,
    report: Any,
    engine: Any,
    allowed: tuple[str, ...],
    paths: tuple[str, ...],
) -> int:
    from Ben.interface.terminal.text import sanitize_terminal_text as clean

    # 授权先于一切：未授权时连探测都不做，退出码 2 与 preflight 保持同一语义。
    if not report.authorized:
        print(clean(f"[FAIL] {report.target} 不在授权范围，未发出任何请求"))
        for probe in report.probes:
            if probe.name == "authorize":
                print(clean(f"       {probe.detail}"))
        print("       把它加进 .env 的 ALLOWED_TARGETS 后重试")
        return 2
    if not report.reachable:
        print(clean(f"[FAIL] {report.target} 不可达，信息收集未开始"))
        for probe in report.probes:
            if probe.name in ("probe-host", "probe-container", "probe-ip"):
                print(clean(f"       {probe.name}: {probe.detail}"))
                if probe.hint:
                    print(clean(f"       ↳ {probe.hint}"))
        print("       先确认服务已启动：ben preflight <target>")
        return 1
    if report.kind not in ("web_application", "ip_address"):
        print(clean(f"[FAIL] recon 只支持 web/ip 目标，当前是 {report.kind or '未知'}"))
        return 1

    channel = _Channel(engine, allowed=allowed, timeout=args.timeout)
    base_url = report.url
    if not channel.in_container:
        # 宿主机视角必须用原始地址：build_targets_info 把 localhost/127.x 改写成了
        # host.docker.internal，那个名字只有容器（且加了 host-gateway）才认。
        from Ben.interface.cli.target_check import _host_view_url

        base_url = _host_view_url(report.target, report.url)
    host = (urlsplit(base_url).hostname or "").lower()

    out_dir = (
        Path(args.out)
        if args.out
        else Path("recon") / (f"{_slug(host)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    )
    raw_dir = None if args.no_write else out_dir / "raw"
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)

    recon = Recon(
        target=report.target,
        base_url=base_url,
        host=host,
        allowed=allowed,
        channel=channel,
        paths=paths,
        max_paths=args.max_paths,
        max_js=args.max_js,
        frontend=not args.no_frontend,
        raw_dir=raw_dir,
    )
    try:
        result = recon.run()
    except GateError as exc:
        print(clean(f"[FAIL] 命令未通过授权/只读闸门：{exc}"))
        return 1

    if not args.no_write:
        _write_artifacts(result, out_dir)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0
