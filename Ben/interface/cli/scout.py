"""``ben scout`` —— 侦察兵：花几分钟买一个判断（sapper 侦察兵的确定性版本）。

为什么在 ``recon`` 之外再加一个命令：

- ``recon`` 回答"**这是什么**"（事实清单）；``scout`` 回答"**该派谁去**"（难度
  裁定 + 可直接写进事实图的 fact 列表）。判定不是事实清单的自然产物，它需要
  一套明确的规则，否则每次都靠人（或模型）临场拍脑袋；
- sapper 的侦察兵本质是"**时间盒 + 定性**"：4 分钟、只侦察、产出
  easy/medium/hard。本命令把这件事变成确定性代码——同样的输入给同样的判定，
  不烧模型额度，结果可 diff、可进 CI；
- 产物与 sapper 的 fact 同构（``kind`` + ``data``），``--emit sapper`` 直接吐出
  ``sapper fact -k service -d host=… -d port=…`` 命令，粘过去就能上板。

与 ``recon`` 的关系：继承 :class:`Ben.interface.cli.recon.Recon`，复用它已经写好
的东西（容器/宿主机通道、闸门、指纹、软 404 基线、路径与前端提取），只新增三
件事：**端口/服务**、**版本台账**、**难度裁定**。

三件新增事各自的诚实边界（重要）：

- 端口：容器内有 nmap 就用 nmap（能区分 open/closed/filtered），没有就退到
  ``nc -z``（只能回答"通/不通"）。两条路都过 Agent 同款闸门，不另开一条能绕过
  白名单的 socket 通路——"CLI 不比 Agent 多一分权限"这条不因为图方便而让步。
- 版本：钉不死是**合法结论**。钉不死时输出 ``payload_policy=no-blind-payload``，
  而不是猜一个版本出来——假版本会导致假 CVE，比没有版本更费时间。
- 难度：是**判断**不是观察，所以每条判定都带 ``signal/weight/toward``，可复核、
  可反驳；它只用于排序，不写进漏洞报告。

用法::

    ben scout http://127.0.0.1:3000/
    ben scout https://staging.example.com --minutes 2 --emit sapper | sh
    ben scout http://10.0.0.5:8080 --no-docker --no-ports --json
    ben scout http://host:3000 --ports 22,80,443,8080 --out /tmp/scout
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from Ben.interface.cli.recon import (
    _DEFAULT_PATHS,
    Fact,
    GateError,
    Recon,
    ReconResult,
    _Channel,
    _load_paths,
    _slug,
    _suggestions,
)

# 默认端口表：只放"一个连接就能定性"的服务，不做全端口爆破。顺序即优先级，
# 时间盒不够时从头截断。web 端口排在前面——真实比赛里 90% 的打击面在 HTTP。
_DEFAULT_PORTS: tuple[int, ...] = (
    80,
    443,
    8080,
    8443,
    8000,
    8888,
    3000,
    5000,
    9090,
    9200,
    22,
    21,
    23,
    445,
    3389,
    3306,
    5432,
    6379,
    11211,
    27017,
    1433,
    1521,
    25,
    110,
    143,
    993,
    389,
    873,
    2375,
    7001,
    8081,
    9000,
)

# nmap 的 file capabilities 与 Docker 默认 bounding set 冲突时（见 _nmap_prefix），
# 用它拷一份不带 xattr 的副本跑 -sT；-sT 是 connect()，本来就不需要特权。
_NMAP_SHIM = "/tmp/.ben-nmap"
_NMAP_REAL = "/usr/lib/nmap/nmap"
_NMAP_DATA = "/usr/share/nmap"

# 逐端口 -sV 最多做多少个：优先端口在前，超出部分只保留开关状态。
_VERSION_MAX_PORTS = 16

# 端口 -> 服务名：容器里没有 nmap 时的兜底标签（nmap 有就以它为准）。
_PORT_SERVICE: dict[int, str] = {
    21: "ftp",
    22: "ssh",
    23: "telnet",
    25: "smtp",
    80: "http",
    110: "pop3",
    143: "imap",
    389: "ldap",
    443: "https",
    445: "smb",
    873: "rsync",
    993: "imaps",
    1433: "mssql",
    1521: "oracle",
    2375: "docker",
    3000: "http-alt",
    3306: "mysql",
    3389: "rdp",
    5000: "http-alt",
    5432: "postgres",
    6379: "redis",
    7001: "weblogic",
    8000: "http-alt",
    8080: "http-alt",
    8081: "http-alt",
    8443: "https-alt",
    8888: "http-alt",
    9000: "http-alt",
    9090: "http-alt",
    9200: "elasticsearch",
    11211: "memcached",
    27017: "mongodb",
}

# 这些端口上的服务通常"先说话"，没 nmap 时可以用一个空连接捞 banner。
_BANNER_PORTS: tuple[int, ...] = (21, 22, 23, 25, 110, 143, 993, 3306, 6379, 11211)

# banner -> (product, version) 的兜底签名。只认能钉死版本的形态，认不出就留原文。
_BANNER_SIGS: tuple[tuple[str, str], ...] = (
    (r"SSH-\d\.\d-OpenSSH[_-]([A-Za-z0-9.]+)", "OpenSSH"),
    (r"220[- ].*?(?:vsFTPd|ProFTPD|Pure-FTPd)\s*([\d.]+)", ""),
    (r"^220[- ](\S+)\s*(?:ESMTP|SMTP)?\s*([\d.]*)", ""),
    (r"Redis|redis_version:([\d.]+)", "Redis"),
)

# 网络服务（非 web）：它们的版本只说明"这台机器上有什么"，不能替代目标应用的版本。
_NETWORK_SERVICES: frozenset[str] = frozenset(
    {
        "ssh",
        "ftp",
        "telnet",
        "smtp",
        "pop3",
        "imap",
        "imaps",
        "pop3s",
        "ldap",
        "smb",
        "rsync",
        "mssql",
        "oracle",
        "mysql",
        "postgresql",
        "rdp",
        "redis",
        "mongodb",
        "memcache",
        "docker",
        "elasticsearch",
    }
)
_NETWORK_PORTS: frozenset[int] = frozenset(
    {
        21,
        22,
        23,
        25,
        110,
        143,
        389,
        445,
        873,
        993,
        1433,
        1521,
        2375,
        3306,
        3389,
        5432,
        6379,
        9200,
        11211,
        27017,
    }
)

# 已知应用：页面标题关键字 -> (产品名, 公开版本接口)。版本接口都是**免认证的只读
# GET**，命中即把 product+version 一起钉死；这也正是 kb052 里"版本必须固定下来"的
# 那条纪律——能一句话问到版本，就不要开始盲打。
_KNOWN_APPS: tuple[tuple[str, str, str], ...] = (
    ("OWASP Juice Shop", "OWASP Juice Shop", "/rest/admin/application-version"),
    ("Grafana", "Grafana", "/api/health"),
    ("Jenkins", "Jenkins", ""),
    ("WordPress", "WordPress", ""),
    ("phpMyAdmin", "phpMyAdmin", ""),
    ("GitLab", "GitLab", ""),
)

# 入口面：路径 -> (类型, 人读名)。只收"能直接开打"的那几条。
_ENTRY_PATHS: dict[str, tuple[str, str]] = {
    "/login": ("auth", "登录页"),
    "/admin": ("admin", "后台入口"),
    "/api": ("api", "接口根"),
    "/api/docs": ("docs", "接口文档"),
    "/swagger-ui/": ("docs", "Swagger UI"),
    "/openapi.json": ("docs", "OpenAPI 描述"),
    "/v3/api-docs": ("docs", "OpenAPI 描述"),
    "/graphql": ("api", "GraphQL 端点"),
    "/actuator": ("debug", "Spring Actuator"),
    "/actuator/health": ("debug", "Actuator 健康端点"),
    "/console": ("debug", "控制台"),
    "/phpinfo.php": ("debug", "phpinfo"),
    "/metrics": ("debug", "指标端点"),
    "/debug": ("debug", "调试端点"),
    "/status": ("debug", "状态页"),
    "/server-status": ("debug", "Apache 状态页"),
}

# 侦察兵的路径顺序与 recon 不同：**入口面优先**。
#
# recon 的表是"指纹→目录→泄露→参数"，先看泄露先看目录；侦察兵只关心"该派谁去"，
# 而判定要用的恰是 /login /actuator /api 这些入口。若沿用 recon 的顺序，一旦
# ``--max-paths`` 截断或时间盒不够，最先被丢掉的正是判定最需要的几条——判定会
# 在没有任何迹象的情况下从 easy 掉成 hard，且看不出是被截断导致的。
_SCOUT_PATHS: tuple[str, ...] = tuple(_ENTRY_PATHS) + tuple(
    path for path in _DEFAULT_PATHS if path not in _ENTRY_PATHS
)

# 命中即"有现成打法的组件"：版本一旦钉死，这类组件就是最便宜的旗子。
_HOT_PRODUCTS: tuple[str, ...] = (
    "fastjson",
    "log4j",
    "shiro",
    "struts",
    "weblogic",
    "jenkins",
    "gitlab",
    "nacos",
    "grafana",
    "phpmyadmin",
    "tomcat",
    "spring",
    "confluence",
    "jira",
    "nexus",
    "drupal",
    "wordpress",
    "thinkphp",
    "laravel",
    "redis",
    "elasticsearch",
    "minio",
    "actuator",
)

# 网络服务本身就是入口的那几个：见到即算一条入口面。
_SERVICE_ENTRIES: dict[str, str] = {
    "ftp": "匿名/弱口令 FTP",
    "telnet": "明文远程登录",
    "smb": "SMB 共享",
    "rdp": "远程桌面",
    "mysql": "数据库直连",
    "postgres": "数据库直连",
    "redis": "Redis 未授权/弱口令",
    "memcached": "Memcached 未授权",
    "mongodb": "MongoDB 未授权",
    "mssql": "数据库直连",
    "docker": "Docker API",
    "elasticsearch": "ES 未授权",
}

# WAF/CDN 特征：命中说明"你能打到的可能只是一层壳"，判定要往难的一侧压。
_WAF_SIGNS: tuple[str, ...] = (
    "cloudflare",
    "akamai",
    "sucuri",
    "bigip",
    "imperva",
    "incapsula",
    "awselb",
    "阿里云",
    "腾讯云",
    "安全狗",
    "长亭",
)

_MAX_PORTS_HARD = 200
_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


# ---------------------------------------------------------------------------
# 命令构造（每一条都必须能过 Agent 同款闸门）
# ---------------------------------------------------------------------------


def scan_script(host: str, ports: tuple[int, ...], *, timeout: float, nmap: str = "nmap") -> str:
    """快速端口扫描：只要状态，不做服务识别。

    ``-sT`` 用 connect() 而不是原始包：容器没有 NET_RAW 也跑得起来，且行为与
    "我能不能连上"完全一致。``-n`` 关掉反解——侦察阶段不需要 PTR，也不该为此
    多花时间。``-oG -`` 走 greppable 输出，一行一台主机，好解析。
    """
    joined = ",".join(str(port) for port in ports)
    return (
        f"{nmap} -Pn -sT -n --max-retries 1 --host-timeout {int(timeout)}s -p {joined} -oG - {host}"
    )


def version_scan_script(
    host: str, ports: tuple[int, ...], *, timeout: float, nmap: str = "nmap"
) -> str:
    """对已确认开放的端口做服务/版本识别。

    用 ``--version-light`` + 逐端口调用：一次扫 8 个端口时，只要其中有一个
    tcpwrapped/filtered 的服务探测卡住，``--host-timeout`` 就会把**整批**掐掉，
    nmap 连 ``Ports:`` 行都不输出（实测 32 个开放端口、0 个版本）。一个端口一次
    10 秒，坏端口只毁掉它自己。
    """
    joined = ",".join(str(port) for port in ports)
    return (
        f"{nmap} -Pn -sT -n -sV --version-light --max-retries 1 "
        f"--host-timeout {int(timeout)}s -p {joined} -oG - {host}"
    )


def nc_probe_script(host: str, port: int, *, timeout: float) -> str:
    """没有 nmap 时的退路：``nc -z`` 只回答"通/不通"。

    刻意不做 ``-w`` 之外的任何花样：netcat 是闸门认得的网络客户端，主机名会被
    提取出来做白名单校验；换 ``/dev/tcp`` 之类的写法能让闸门看不见目标，那等于
    给 CLI 开一条绕过通道，不做。
    """
    return f"nc -z -w {max(1, int(timeout))} {host} {port}"


def banner_script(host: str, port: int, *, timeout: float) -> str:
    """空连接捞 banner：只对"服务端先说话"的端口用。"""
    return f"nc -w {max(1, int(timeout))} {host} {port} </dev/null"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


@dataclass
class PortObservation:
    port: int
    proto: str = "tcp"
    state: str = "unknown"
    service: str = ""
    product: str = ""
    version: str = ""
    banner: str = ""
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "proto": self.proto,
            "state": self.state,
            "service": self.service,
            "product": self.product,
            "version": self.version,
            "banner": self.banner,
            "evidence": self.evidence,
        }


def _json_version(body: str) -> str:
    """从版本接口的响应里取版本号。JSON 与 ``"version": "x"`` 两种形态都认。"""
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        for key in ("version", "Version", "appVersion", "buildVersion"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:40]
    match = re.search(r'"(?:version|Version)"\s*:\s*"([^"]{1,40})"', text)
    return match.group(1).strip() if match else ""


def _split_product_version(raw: str) -> tuple[str, str]:
    """把 nmap/banner 的 ``"nginx 1.24.0"`` 拆成 (product, version)。

    规则：从左往右找第一个"带点的数字"，它前面的是产品名（可是多个词，
    ``Apache httpd 2.4.41``），它是版本号。找不到就整串当产品、版本留空——
    宁可没有版本，也不截半个版本号出来。
    """
    text = raw.strip().replace("/", " ")
    if not text:
        return "", ""
    tokens = text.split()
    for index, token in enumerate(tokens):
        if re.match(r"^v?\d+(?:\.\d+)+", token):
            product = " ".join(tokens[:index]) or token
            return product.strip(), token.lstrip("v")
    return text, ""


def _parse_nmap_greppable(text: str) -> list[PortObservation]:
    """解析 ``nmap -oG -`` 的 Ports 行。

    行形态：``Ports: 22/open/tcp//ssh//OpenSSH 8.9p1/, 80/open/tcp//http//nginx 1.24.0/``
    字段用 ``/`` 分隔，第 0 位端口、第 1 位状态、第 2 位协议、第 4 位服务名、
    第 6 位版本串。版本串里可能出现空格与括号，所以按"下一个条目"切分而不是
    按逗号切分。
    """
    observations: list[PortObservation] = []
    for line in text.splitlines():
        if "Ports:" not in line:
            continue
        blob = line.split("Ports:", 1)[1].strip().rstrip(",")
        items = re.split(r",\s*(?=\d+/(?:open|closed|filtered)/)", blob)
        for item in items:
            fields = item.strip().split("/")
            if len(fields) < 3 or not fields[0].isdigit():
                continue
            state = fields[1].strip().lower()
            proto = (fields[2] or "tcp").strip().lower()
            service = fields[4].strip() if len(fields) > 4 else ""
            raw_version = fields[6].strip() if len(fields) > 6 else ""
            product, version = _split_product_version(raw_version)
            observations.append(
                PortObservation(
                    port=int(fields[0]),
                    proto=proto or "tcp",
                    state=state,
                    service=service or _PORT_SERVICE.get(int(fields[0]), ""),
                    product=product,
                    version=version,
                    evidence=f"nmap -sV {raw_version}".strip(),
                )
            )
    return observations


def _banner_product_version(banner: str) -> tuple[str, str]:
    """从 banner 文本里认产品与版本；认不出返回 ("", "")。"""
    text = banner.strip()
    if not text:
        return "", ""
    for pattern, forced in _BANNER_SIGS:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if not match:
            continue
        groups = [group for group in match.groups() if group]
        if forced and groups:
            return forced, groups[0]
        if len(groups) >= 2:
            return groups[0], groups[1]
        if groups:
            product, version = _split_product_version(groups[0])
            return product, version
    # 认不出具体产品时，至少把版本号捞出来——这是唯一值得留下的信息。
    match = re.search(r"\b(\d+\.\d+(?:\.\d+)?[a-z]?\d*)\b", text)
    return "", match.group(1) if match else ""


def _waf_signal(result: ReconResult) -> str:
    for fact in result.of_kind("http"):
        if fact.title != "根路径响应":
            continue
        haystack = f"{fact.data.get('server', '')} {fact.data.get('powered_by', '')}".lower()
        for sign in _WAF_SIGNS:
            if sign in haystack:
                return sign
    return ""


# ---------------------------------------------------------------------------
# 版本台账
# ---------------------------------------------------------------------------


def _service_role(service: str, port: int | None) -> str:
    """把一条版本证据归到 "web" 还是 "network"。

    这个划分不是分类癖：``OpenSSH 8.9p1`` 钉死了**不代表**3000 端口那个应用钉死了，
    但旧实现把两者混在一本台账里，任何一条 ssh 版本都会把 ``version_locked``
    置真，于是"未识别的 web 应用"被当成"已有 CVE 可查"，直接派普通士兵上去。
    """
    name = (service or "").lower()
    if name.startswith("http") or name in ("https", "http-alt", "http-proxy", "ssl/http"):
        return "web"
    if name in _NETWORK_SERVICES:
        return "network"
    if port in _NETWORK_PORTS:
        return "network"
    return "web"


def _version_ledger(result: ReconResult) -> list[dict[str, Any]]:
    """把散落在各处的版本证据合成一本台账：一个产品一行，取最高置信度。

    置信度口径（刻意保守）：

    - ``high``   —— 服务自己报的版本（nmap -sV、Server/X-Powered-By 带版本）；
    - ``medium`` —— 产品身份确定、版本来自正文特征（如 favicon/模板路径）；
    - ``low``    —— 只认出产品，版本未知。

    只有 high/medium 才算"钉死"：low 是一个提醒，不是一个可用结论。每行额外带
    ``role``（web / network），钉死判定只看 web 面——见 :func:`_service_role`。
    """
    ledger: dict[str, dict[str, Any]] = {}

    def merge(
        product: str, version: str, confidence: str, evidence: str, role: str = "web"
    ) -> None:
        name = (product or "").strip()
        if not name:
            return
        key = name.lower()
        entry = ledger.setdefault(
            key,
            {
                "product": name,
                "version": "",
                "confidence": "low",
                "evidence": [],
                "role": role,
            },
        )
        better = _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[entry["confidence"]]
        if version and (not entry["version"] or better):
            entry["version"] = version
        if better:
            entry["confidence"] = confidence
        if evidence and evidence not in entry["evidence"]:
            entry["evidence"].append(evidence[:160])

    for fact in result.facts:
        if fact.kind != "service":
            continue
        if fact.title == "技术栈指纹":
            for item in fact.data.get("items", []):
                version = str(item.get("version") or "")
                # 响应头/正文里带版本都算服务自报；无版本只能算 low。
                merge(
                    str(item.get("name") or ""),
                    version,
                    "high" if version else "low",
                    f"http-fingerprint:{item.get('category', '')}",
                    "web",
                )
            continue
        product = str(fact.data.get("product") or "")
        version = str(fact.data.get("version") or "")
        if product or version:
            port = fact.data.get("port")
            merge(
                product or fact.data.get("service", ""),
                version,
                "high" if version else "low",
                str(fact.data.get("evidence") or "port"),
                _service_role(str(fact.data.get("service") or product), port)
                if isinstance(port, int)
                else "web",
            )
    return sorted(
        ledger.values(), key=lambda item: (-_CONFIDENCE_RANK[item["confidence"]], item["product"])
    )


def _version_locked(ledger: list[dict[str, Any]]) -> bool:
    """只有 **web 面** 的版本算"钉死"。

    旁路服务（ssh/mysql/redis…）的版本对目标应用没有替代性：知道 OpenSSH 8.9p1
    并不让 Juice Shop 的 payload 变得更精准。早期版本把两者混算，结果一台开着
    22 的机器就能把"web 应用完全未识别"判成 easy 并派普通士兵——这是错的方向。
    """
    return any(
        item["version"]
        and item["confidence"] in ("high", "medium")
        and item.get("role", "web") == "web"
        for item in ledger
    )


def _payload_policy(locked: bool) -> dict[str, str]:
    """版本策略写进产物，而不是写在文档里——文档会被忽略，字段不会。"""
    if locked:
        return {
            "policy": "targeted-cve-ok",
            "why": "版本已钉死：按 product+version 检索对应 CVE，不要再用通用 payload 试",
        }
    return {
        "policy": "no-blind-payload",
        "why": (
            "版本未钉死：盲打 payload 只会制造噪音。先按 kb052 补指纹——响应头/cookie 名 → "
            "favicon hash → 报错页堆栈 → /actuator,/package.json,/composer.json → whatweb"
        ),
    }


# ---------------------------------------------------------------------------
# 入口面
# ---------------------------------------------------------------------------


def _entry_face(result: ReconResult) -> list[dict[str, Any]]:
    """入口面 = "攻击者第一步会点哪儿"。只收有证据的，不猜。"""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, where: str, evidence: str, status: Any = None) -> None:
        key = f"{kind}:{where}"
        if key in seen:
            return
        seen.add(key)
        entries.append({"type": kind, "where": where, "evidence": evidence, "status": status})

    for fact in result.of_kind("surface"):
        if fact.data.get("fallback") or fact.data.get("baseline"):
            continue
        mapping = _ENTRY_PATHS.get(fact.title)
        status = fact.data.get("status")
        if not mapping or not isinstance(status, int):
            continue
        if status in (200, 401, 403):
            add(mapping[0], fact.title, "路径探测", status)
        elif 500 <= status < 600:
            # 5xx 不是"端点可用"，是"路由存在但处理失败"：路径一定命中过某个
            # handler（否则会落到兜底页）。这类端点常带着报错堆栈或注入面，
            # 但也不能按 debug 端点那种强度加分，所以单独一类。
            add("error", fact.title, "路由存在但处理失败", status)

    for fact in result.of_kind("param"):
        if fact.title != "页面表单":
            continue
        forms = fact.data.get("forms", [])
        if any(item.get("has_password") for item in forms):
            action = next((item.get("action") for item in forms if item.get("has_password")), "")
            add("auth", action or "(根页表单)", "password 字段", 200)

    for fact in result.of_kind("endpoint"):
        for path in fact.data.get("items", []):
            lowered = str(path).lower()
            if "upload" in lowered:
                add("upload", str(path), "前端引用", None)
            elif lowered.startswith(("/api/", "/v1/", "/v2/", "/rest/")):
                add("api", str(path), "前端引用", None)

    for fact in result.of_kind("service"):
        service = str(fact.data.get("service") or "").lower()
        if service in _SERVICE_ENTRIES and fact.data.get("state") == "open":
            add(
                "network",
                f"{fact.data.get('port')}/{fact.data.get('proto', 'tcp')} {service}",
                _SERVICE_ENTRIES[service],
                None,
            )
    return entries


# ---------------------------------------------------------------------------
# 难度裁定
# ---------------------------------------------------------------------------


def _verdict(
    result: ReconResult,
    *,
    ledger: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    observations: list[PortObservation],
    elapsed: float,
    minutes: float,
) -> dict[str, Any]:
    """把信号打成两个方向的分，再落成 easy/medium/hard。

    这不是"预测"，是"排序"：同样的信号给同样的分，每条都写清方向与权重，
    读的人可以不同意并直接改权重。刻意只做确定性规则——难度判定一旦交给模型，
    就无法解释为什么这道题被判成 hard，调度也就没法复盘。
    """
    easy = 0
    hard = 0
    signals: list[dict[str, Any]] = []

    def add(toward: str, weight: int, signal: str) -> None:
        nonlocal easy, hard
        if toward == "easy":
            easy += weight
        else:
            hard += weight
        signals.append({"toward": toward, "weight": weight, "signal": signal})

    locked = _version_locked(ledger)
    debug_entries = [item for item in entries if item["type"] in ("debug", "docs")]
    error_entries = [item for item in entries if item["type"] == "error"]
    upload_entries = [item for item in entries if item["type"] == "upload"]
    auth_entries = [item for item in entries if item["type"] == "auth"]
    open_ports = [obs for obs in observations if obs.state == "open"]
    network_entries = [item for item in entries if item["type"] == "network"]
    confirmed_paths = [
        item for item in entries if item.get("evidence") in ("路径探测", "路由存在但处理失败")
    ]

    if debug_entries:
        names = "、".join(item["where"] for item in debug_entries[:3])
        add("easy", 3, f"调试/文档端点暴露（{names}）：攻击路径直接可见")
    if error_entries:
        names = "、".join(item["where"] for item in error_entries[:3])
        add("easy", 1, f"端点返回 5xx（{names}）：路由存在、处理失败，报错面值得看一眼")
    if locked:
        web_versions = [
            item
            for item in ledger
            if item["version"]
            and item["confidence"] in ("high", "medium")
            and item.get("role", "web") == "web"
        ] or [
            item for item in ledger if item["version"] and item["confidence"] in ("high", "medium")
        ]
        versions = "、".join(f"{item['product']}/{item['version']}" for item in web_versions)
        add("easy", 2, f"版本已钉死（{versions[:80]}）：可直接检索对应 CVE")
    else:
        add("hard", 2, "版本未钉死：没有 product+version 就没有可检索的 CVE，先补指纹")
    if auth_entries:
        names = "、".join(item["where"] for item in auth_entries[:2])
        add("easy", 2, f"存在认证面（{names}）：弱口令/认证绕过是常备路径")
    if upload_entries:
        add("easy", 2, f"存在上传面（{upload_entries[0]['where']}）：文件上传类漏洞优先")
    hot = [
        item["product"]
        for item in ledger
        if any(name in item["product"].lower() for name in _HOT_PRODUCTS)
    ]
    if hot and locked:
        add("easy", 3, f"命中自带打法的组件（{'、'.join(hot[:3])}）")
    if not entries:
        add("hard", 3, "未发现任何入口面：要么全站静态，要么入口在认证后")
    if len(open_ports) >= 3 and len(network_entries) >= 2:
        add("hard", 2, f"多服务（{len(open_ports)} 个开放端口）：可能需要链式或横向")
    waf = _waf_signal(result)
    if waf:
        add("hard", 2, f"疑似 WAF/CDN（{waf}）：打到的是壳不是应用")
    baseline = next((fact for fact in result.of_kind("surface") if fact.data.get("baseline")), None)
    fallbacks = [fact for fact in result.of_kind("surface") if fact.data.get("fallback")]
    soft_404 = bool(baseline and baseline.data.get("status") == 200 and fallbacks)
    if baseline and baseline.data.get("status") == 200 and len(fallbacks) >= 3:
        if confirmed_paths:
            # 有路径与基线逐字节不同 → 探测本身是有效的，不能因为"大部分没打中"
            # 就整片否定；但没打中的那些必须明确作废，否则会被当成否定证据。
            names = "、".join(item["where"] for item in confirmed_paths[:3])
            result.notes.append(
                f"全站兜底页（{len(fallbacks)} 条命中与随机基线逐字节相同）与有效命中并存："
                f"{names} 等 {len(confirmed_paths)} 条与基线不同 → 只信这些，其余未命中路径一律作废"
            )
        else:
            add("hard", 1, f"全站兜底页（{len(fallbacks)} 条路径逐字节相同）：路径类结论不可信")
    gated = [
        fact
        for fact in result.of_kind("surface")
        if isinstance(fact.data.get("status"), int) and fact.data["status"] in (401, 403)
    ]
    if len(gated) >= 3:
        add("hard", 1, f"多个端点需认证（{len(gated)} 条 401/403）：入口在认证之后")

    if easy >= 3 and easy - hard >= 2:
        level = "easy"
    elif hard >= 3 and hard - easy >= 2:
        level = "hard"
    else:
        level = "medium"

    if locked and entries:
        confidence = "high"
    elif soft_404 and not locked and not confirmed_paths:
        # 全站兜底页时"没找到入口"不代表没有入口——这类测量本身不可信，
        # 只能要求重测，不能直接判成 hard 派将军上去。
        confidence = "low"
    else:
        confidence = "medium"

    next_step = {
        "easy": "soldier（普通士兵：按版本查 CVE 直接打）",
        "medium": "soldier + 15 分钟复查（先派普通士兵，卡住再升级）",
        "hard": "general（将军：自带雇兵权，按链式打法推进）",
    }[level]
    if confidence == "low":
        next_step = "scout-again（换个角度再侦察一次：当前证据不足以下判定）"

    return {
        "level": level,
        "confidence": confidence,
        "score_easy": easy,
        "score_hard": hard,
        "next_step": next_step,
        "version_locked": locked,
        "entry_count": len(entries),
        "open_ports": len(open_ports),
        "elapsed_seconds": round(elapsed, 1),
        "minutes_budget": minutes,
        "timed_out": elapsed >= minutes * 60,
        "signals": signals,
        "reason": "；".join(item["signal"] for item in signals) or "无可用信号",
    }


# ---------------------------------------------------------------------------
# fact 导出（sapper / jsonl）
# ---------------------------------------------------------------------------

_SAPPER_KIND = {
    "host": "host",
    "service": "service",
    "vuln": "vuln",
    "loot": "loot",
    "cred": "cred",
}

# 与版本台账重复、且不属于"某端口上的服务"的聚合事实：不导出到 sapper。
_SKIP_FOR_SAPPER = ("技术栈指纹",)


def _flatten_value(value: Any) -> str:
    """把 fact 的 data 值压成 sapper ``-d k=v`` 能接受的标量。

    嵌套结构直接丢掉而不是 json 序列化：``-d`` 是扁平键值，塞一段 JSON 进去
    只会让下游 grep 不到任何东西。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        scalars = [
            item
            for item in value
            if isinstance(item, (str, int, float)) and not isinstance(item, bool)
        ]
        if scalars and len(scalars) == len(value):
            return ",".join(str(item) for item in scalars)[:200]
    return ""


def sapper_commands(result: ReconResult, *, limit: int = 200) -> list[str]:
    """把事实清单翻译成 ``sapper fact`` 命令。粘进 shell 就能上板。

    三条刻意规则：

    - 软 404 的路径事实**不导出**：它们与随机基线逐字节相同，写进事实图只会让
      下一个 agent 以为"这个路径存在"。侦察兵上板的应该是结论，不是噪音；
    - 聚合类事实（技术栈指纹）不导出：它和版本台账重复，而 sapper 的
      ``service`` 是"某端口上的某服务"，把一堆没有端口的产品塞进去会把图搞脏；
    - 入口面拆成每入口一条 ``note``：指挥官要按入口派单，一整坨 JSON 没法派。
    """
    commands: list[str] = []

    def emit(description: str, kind: str, data: dict[str, Any]) -> None:
        pairs: list[str] = []
        for key, value in list(data.items())[:10]:
            flat = _flatten_value(value)
            if flat:
                pairs.append(f"-d {shlex.quote(f'{key}={flat}')}")
        if not pairs:
            # 一条没有任何 -d 的 fact 在图上只是个空标题，帮不上任何忙。
            return
        args = " ".join(pairs)
        commands.append(f"sapper fact {shlex.quote(description)} -k {kind} {args}".strip())

    for fact in result.facts:
        if len(commands) >= limit:
            break
        if fact.data.get("fallback") or fact.data.get("baseline"):
            continue
        if fact.title in _SKIP_FOR_SAPPER:
            continue
        if fact.kind == "surface" and fact.title == "入口面":
            for entry in fact.data.get("entries", []):
                emit(
                    f"entry:{entry.get('where', '')}",
                    "note",
                    {
                        "type": entry.get("type", ""),
                        "where": entry.get("where", ""),
                        "evidence": entry.get("evidence", ""),
                        "status": entry.get("status"),
                    },
                )
            continue
        kind = _SAPPER_KIND.get(fact.kind, "note")
        data = dict(fact.data)
        if kind == "service" and "port" not in data:
            # 版本台账/技术栈指纹是主机级结论，不是某个端口上的服务。
            kind = "note"
        if kind == "service" and not data.get("host"):
            data["host"] = result.host
        if kind == "host" and not (data.get("hostname") or data.get("ip")):
            data["hostname"] = result.host
        if fact.kind == "surface" and fact.title.startswith("/"):
            data["path"] = fact.title
        emit(f"{fact.kind}:{fact.title}", kind, data)
    return commands[:limit]


# ---------------------------------------------------------------------------
# 侦察本体
# ---------------------------------------------------------------------------


class _Progress:
    """实时进度打印：侦察要跑好几分钟，终端不能全程无声。

    只在交互模式（非 ``--json`` / ``--emit``）下挂到 Scout 上。每行带已用
    时间戳并强制 ``flush``——进度行的全部意义就在于"这一秒"就能看见，攒着
    缓冲和没有输出是一样糟的。
    """

    def __init__(self) -> None:
        self.started = time.monotonic()

    def stamp(self) -> str:
        elapsed = max(0, int(time.monotonic() - self.started))
        return f"[{elapsed // 60:d}:{elapsed % 60:02d}]"

    def step(self, message: str) -> None:
        print(f"{self.stamp()} ▶ {message}", flush=True)

    def detail(self, message: str) -> None:
        print(f"{self.stamp()}   · {message}", flush=True)


class Scout(Recon):
    """一次侦察兵任务：时间盒内取事实，然后给判定。

    不能写成 ``@dataclass``：父类 :class:`Recon` 的 ``__init__`` 里有真正的初始化
    逻辑（通道、路径表、结果容器），dataclass 生成的 ``__init__`` 会把它整段跳过。
    """

    def __init__(
        self,
        *,
        minutes: float = 4.0,
        ports: tuple[int, ...] = _DEFAULT_PORTS,
        port_scan: bool = True,
        port_timeout: float = 2.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.minutes = minutes
        self.ports = ports
        self.port_scan = port_scan
        self.port_timeout = port_timeout
        self.deadline = 0.0
        self.observations: list[PortObservation] = []
        self.ledger: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.verdict: dict[str, Any] = {}
        self._port_facts: dict[int, Fact] = {}
        self._have_nmap: bool | None = None
        self._have_nc: bool | None = None
        self._nmap_checked = False
        self._nmap_bin: str | None = None
        self._budget_noted = False
        self.progress: _Progress | None = None

    def _say(self, message: str) -> None:
        """发一行进度；``--json``/``--emit`` 模式下 progress 为 None，静默。"""
        if self.progress is not None:
            self.progress.step(message)

    # -- 时间盒 ------------------------------------------------------------

    def _remaining(self) -> float:
        return self.deadline - time.monotonic()

    def _exhausted(self) -> bool:
        if self._remaining() <= 0:
            if not self._budget_noted:
                self.result.notes.append(
                    f"时间盒用尽（{self.minutes:g} 分钟）：后续步骤已跳过，判定基于已取到的证据"
                )
                self._budget_noted = True
            return True
        return False

    def _fit_paths_budget(self) -> None:
        """把路径条数压到时间盒还装得下的规模（每条按约 1.2 秒估）。

        侦察兵的价值是"到点交付一个判断"，不是"把路径表跑完"。宁可少探 10 条
        路径，也不要因为超时把判定丢掉。
        """
        affordable = int(max(0.0, self._remaining()) / 1.2)
        if affordable < self.max_paths:
            self.max_paths = affordable
            if affordable == 0:
                self.result.notes.append("时间盒不足以做路径探测：已跳过（结论只基于端口与根路径）")

    # -- 主流程 ------------------------------------------------------------

    def run(self) -> ReconResult:
        started = time.monotonic()
        self.deadline = started + self.minutes * 60
        self._say(f"时间盒 {self.minutes:g} 分钟，开始侦察")
        self._say("主机解析（DNS）")
        self._step_dns()
        if not self._exhausted():
            self._say("根路径与响应头")
            self._step_root()
        if not self._exhausted():
            self._say("应用版本接口（页面标题 → 已知版本端点）")
            self._step_app_version()
        if not self._exhausted():
            self._say("Web 指纹（响应头 / 正文特征）")
            self._step_web_service()
        if not self._exhausted():
            self._say("TLS 证书")
            self._step_tls()
        if self.port_scan and not self._exhausted():
            self._step_ports()
        if not self._exhausted() and self._root is not None and self._root.ok:
            self._fit_paths_budget()
            before = len(self.result.of_kind("surface"))
            self._say("路径探测（入口面优先）")
            self._step_paths()
            self._say(f"路径探测完成：命中 {len(self.result.of_kind('surface')) - before} 条")
        if not self._exhausted() and self.frontend and self._root is not None and self._root.ok:
            self._say("前端资源（JS 里的接口与密钥）")
            self._step_frontend()
        self._say("汇总：版本台账 + 入口面 + 难度裁定")
        self._step_version_lock()
        self._step_entry_face()
        elapsed = time.monotonic() - started
        self.verdict = _verdict(
            self.result,
            ledger=self.ledger,
            entries=self.entries,
            observations=self.observations,
            elapsed=elapsed,
            minutes=self.minutes,
        )
        self.result.add(Fact("note", "难度裁定", self.verdict, "verdict"))
        self.result.suggestions = _suggestions(self.result)
        return self.result

    # -- 端口与服务 --------------------------------------------------------

    def _step_web_service(self) -> None:
        """把目标 URL 本身记成一条 service 事实。

        端口扫描可以跳过（``--no-ports``、容器里没 nmap、时间盒不够），但"我连的
        这个 80/443 上跑的是什么"永远有答案——它就是刚才那次根路径响应。sapper
        侦察兵第一步要的 ``-k service -d host=… -d port=…`` 必须无条件存在，否则
        后面所有版本结论都没有宿主。
        """
        parsed = urlsplit(self.base_url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        if any(obs.port == port and obs.state == "open" for obs in self.observations):
            return  # 端口扫描已经给了更准的版本（-sV 比 Server 头可信）
        root = self._root
        product, version = "", ""
        if root is not None and not root.error:
            product, version = _split_product_version(root.header("server"))
            if not product:
                product, version = _split_product_version(root.header("x-powered-by"))
        host = parsed.hostname or self.host
        data: dict[str, Any] = {
            "host": host,
            "port": port,
            "proto": "tcp",
            "state": "open",
            "service": parsed.scheme.lower() or "http",
            "evidence": "http-root",
        }
        if product:
            data["product"] = product
        if version:
            data["version"] = version
        fact = Fact("service", f"{host}:{port}/tcp", data, "http-root")
        self._port_facts[port] = fact
        self.result.add(fact)

    def _tool_available(self, name: str) -> bool:
        cache = {"nmap": self._have_nmap, "nc": self._have_nc}
        if cache.get(name) is not None:
            return bool(cache[name])
        _, _, code = self.channel.shell(f"command -v {name}", timeout=5)
        available = code == 0
        if name == "nmap":
            self._have_nmap = available
        else:
            self._have_nc = available
        return available

    def _nmap_prefix(self) -> str | None:
        """返回可用的 nmap 调用前缀；``None`` 表示这个容器里根本跑不了 nmap。

        ``command -v nmap`` 不够：strix 镜像里 ``/usr/bin/nmap`` 是个 shell
        wrapper，真正的二进制带 ``cap_net_raw,cap_net_admin,cap_net_bind_service``
        的 file capability，而 Docker 默认 bounding set **不含 NET_ADMIN**，于是
        ``execve`` 直接返回 EPERM（shell 报 exit 126、`Operation not permitted`）。
        这种情况下 nmap 一次也没跑成，端口清单却是空的——必须发现它，而不是把
        "扫不出来"当成"没有开放端口"。

        修法不去改镜像、也不加 ``--cap-add NET_ADMIN``（那是给沙箱放权）：把二进制
        拷一份到 ``/tmp``，副本没有 xattr 就不会触发能力检查；``-sT`` 是 connect
        扫描，本来不需要任何特权。
        """
        if self._nmap_checked:
            return self._nmap_bin
        self._nmap_checked = True
        out, err, code = self.channel.shell("nmap --version", timeout=10)
        if code == 0 and "Nmap version" in out:
            self._nmap_bin = "nmap"
            return self._nmap_bin
        reason = ((err or out).strip().splitlines() or ["无输出"])[-1][:120]
        shim = (
            f"cp -f {_NMAP_REAL} {_NMAP_SHIM} && chmod 700 {_NMAP_SHIM} && "
            f"NMAPDIR={_NMAP_DATA} {_NMAP_SHIM} --version"
        )
        out, err, code = self.channel.shell(shim, timeout=25)
        if code == 0 and "Nmap version" in out:
            self._nmap_bin = f"NMAPDIR={_NMAP_DATA} {_NMAP_SHIM}"
            self.result.notes.append(
                f"nmap 在容器内不可直接执行（{reason}）：已改用无特权副本 "
                f"{_NMAP_SHIM}（-sT 不需要特权；扫描结果等价）"
            )
            return self._nmap_bin
        self.result.notes.append(f"nmap 不可用（{reason}），端口推断改用 nc -z")
        return None

    def _step_ports(self) -> None:
        host = urlsplit(self.base_url).hostname or self.host
        ports = self.ports[: min(len(self.ports), _MAX_PORTS_HARD)]
        if not ports:
            return
        budget = max(5.0, min(self._remaining() - 5, 45.0))
        nmap = self._nmap_prefix()
        if nmap:
            self.result.notes.append("端口探测：nmap -sT（可区分 open/closed/filtered）")
            self._say(f"端口扫描 nmap -sT：{len(ports)} 个端口，预算 {budget:.0f}s")
            stdout, stderr, code = self.channel.shell(
                scan_script(host, ports, timeout=budget, nmap=nmap), timeout=budget + 5
            )
            observations = _parse_nmap_greppable(stdout)
            if code != 0 and not observations:
                self.result.notes.append(
                    f"nmap 端口扫描失败（exit={code}）：{(stderr or '').strip()[:160] or '无输出'}"
                )
            if observations:
                self._record_ports(host, observations, source="nmap")
                open_ports = tuple(obs.port for obs in observations if obs.state == "open")
                self._say(
                    f"端口扫描完成：开放 {len(open_ports)} / 共探测 {len(observations)} 个"
                    f"（{', '.join(str(port) for port in open_ports[:8])}"
                    f"{'…' if len(open_ports) > 8 else ''}）"
                )
                if open_ports:
                    self._step_service_versions(host, open_ports, nmap)
                return
        if self._tool_available("nc"):
            self.result.notes.append(
                "端口探测：容器内无 nmap，退到 nc -z（只能回答通/不通，无法区分 closed 与 filtered）"
            )
            self._say(f"端口探测 nc -z：{len(ports)} 个端口（无 nmap 的降级路径）")
            observations = []
            for port in ports:
                if self._exhausted():
                    break
                _, _, code = self.channel.shell(
                    nc_probe_script(host, port, timeout=self.port_timeout),
                    timeout=self.port_timeout + 5,
                )
                observations.append(
                    PortObservation(
                        port=port,
                        proto="tcp",
                        state="open" if code == 0 else "closed",
                        service=_PORT_SERVICE.get(port, ""),
                        evidence="nc -z",
                    )
                )
                if self.progress is not None:
                    self.progress.detail(f"nc {port}: {'开放' if code == 0 else '不通'}")
            self._record_ports(host, observations, source="nc")
            self._step_banners(host, [obs.port for obs in observations if obs.state == "open"])
            return
        self.result.notes.append(
            "端口探测跳过：容器内既无 nmap 也无 nc（不降级到宿主机裸 socket：那会绕过授权闸门）"
        )

    def _step_service_versions(self, host: str, open_ports: tuple[int, ...], nmap: str) -> None:
        """逐端口做 ``-sV``，按端口表的优先级排序、受时间盒约束。

        为什么不一整批发给 nmap：多个 tcpwrapped/过滤端口混在一批里时，
        ``--host-timeout`` 会把**整批**掐掉，连 ``Ports:`` 行都不输出——实测
        32 个开放端口、0 个版本。一个端口一次 10 秒，坏端口只毁掉它自己
        （实测 ssh 0.5s、nginx 6.4s、tcpwrapped 5.3s）。
        """
        order = {port: index for index, port in enumerate(self.ports)}
        ranked = sorted(open_ports, key=lambda port: order.get(port, len(order)))
        found = False
        total = min(len(ranked), _VERSION_MAX_PORTS)
        for scanned, port in enumerate(ranked, 1):
            if scanned > total:
                self.result.notes.append(
                    f"版本识别按优先级只做了前 {_VERSION_MAX_PORTS} 个端口，"
                    f"其余 {len(ranked) - total} 个只保留开关状态"
                )
                break
            if self._remaining() <= 15:
                self.result.notes.append(
                    f"时间盒不足：剩余 {len(ranked) - scanned + 1} 个端口没做版本识别（端口状态仍然有效）"
                )
                break
            vbudget = 10.0
            if self.progress is not None:
                self.progress.detail(f"-sV 端口 {port}（{scanned}/{total}）…")
            vout, _, vcode = self.channel.shell(
                version_scan_script(host, (port,), timeout=vbudget, nmap=nmap),
                timeout=vbudget + 8,
            )
            versioned = _parse_nmap_greppable(vout) if vcode == 0 and vout.strip() else []
            self._record_ports(host, versioned, source="nmap -sV", merge=True)
            if versioned and self.progress is not None:
                best = next((obs for obs in versioned if obs.product or obs.version), None)
                if best is not None:
                    label = f"{best.product or best.service} {best.version}".strip()
                    self.progress.detail(f"  ↳ {port}: {label}")
                    found = True
                else:
                    self.progress.detail(f"  ↳ {port}: 无版本（tcpwrapped/过滤常见）")
            found = found or any(obs.product or obs.version for obs in versioned)
        if not found:
            self.result.notes.append(
                "nmap -sV 没给出任何版本（加固或超时都会这样）：端口是确定的，"
                "版本必须另找证据，别当成「这些服务没有版本」"
            )

    def _record_ports(
        self,
        host: str,
        observations: list[PortObservation],
        *,
        source: str,
        merge: bool = False,
    ) -> None:
        """落库：第一次扫描加事实，第二次（-sV）只更新同一条，不重复加。

        重复加会产生两条 ``22/tcp`` 的 service 事实，下游按端口聚合时会算成两台
        服务——事实图里"一件事一条"比"多写几条"重要。
        """
        by_port = {obs.port: obs for obs in self.observations}
        for obs in observations:
            existing = by_port.get(obs.port)
            if merge and existing is not None:
                for attr in ("service", "product", "version", "banner"):
                    value = getattr(obs, attr)
                    if not value:
                        continue
                    # 别让 nmap 的 services 表名把已经确认的 http 覆盖成 "ppp"
                    # （3000 端口就是这么被叫成 ppp 的）：HTTP 面是更强的证据。
                    if (
                        attr == "service"
                        and str(existing.service).startswith("http")
                        and not str(value).startswith("http")
                    ):
                        continue
                    setattr(existing, attr, value)
                if obs.state != "unknown":
                    existing.state = obs.state
                if obs.evidence:
                    existing.evidence = obs.evidence
            else:
                self.observations.append(obs)
                by_port[obs.port] = obs
            merged = by_port[obs.port]
            if merged.state != "open":
                continue
            data: dict[str, Any] = {
                "host": host,
                "port": merged.port,
                "proto": merged.proto,
                "state": merged.state,
                "service": merged.service,
                "evidence": merged.evidence or source,
            }
            if merged.product:
                data["product"] = merged.product
            if merged.version:
                data["version"] = merged.version
            if merged.banner:
                data["banner"] = merged.banner[:200]
            fact = self._port_facts.get(merged.port)
            if fact is None:
                fact = Fact("service", f"{host}:{merged.port}/{merged.proto}", data, source)
                self._port_facts[merged.port] = fact
                self.result.add(fact)
            else:
                fact.data = data
                fact.source = source
        closed = [obs.port for obs in observations if obs.state != "open"]
        if closed and not merge:
            self.result.add(
                Fact(
                    "note",
                    "未开放的端口（否定证据）",
                    {"ports": closed, "source": source},
                    source,
                )
            )

    def _step_banners(self, host: str, ports: list[int]) -> None:
        for port in [item for item in ports if item in _BANNER_PORTS]:
            if self._exhausted():
                return
            stdout, _, code = self.channel.shell(
                banner_script(host, port, timeout=self.port_timeout),
                timeout=self.port_timeout + 5,
            )
            if code != 0 or not stdout.strip():
                continue
            banner = stdout.strip().splitlines()[0][:200]
            product, version = _banner_product_version(banner)
            obs = next((item for item in self.observations if item.port == port), None)
            if obs is None:
                continue
            obs.banner = banner
            obs.product = obs.product or product
            obs.version = obs.version or version
            for fact in self.result.facts:
                if fact.kind == "service" and fact.data.get("port") == port:
                    fact.data["banner"] = banner
                    if obs.product:
                        fact.data["product"] = obs.product
                    if obs.version:
                        fact.data["version"] = obs.version

    # -- 版本与入口 --------------------------------------------------------

    def _step_app_version(self) -> None:
        """认出已知应用就问一句版本：一个免认证 GET，胜过后面所有盲打。

        版本"钉死"的价值在于把搜索空间从"所有漏洞"缩到"这个版本的漏洞"。页面标题
        已经暴露了产品身份，而多数产品的版本接口是公开只读的——这一步的性价比比
        任何指纹插件都高。
        """
        root = next(
            (fact for fact in self.result.of_kind("http") if fact.title == "根路径响应"), None
        )
        title = str((root.data.get("title") if root else "") or "")
        if not title:
            return
        for needle, product, path in _KNOWN_APPS:
            if needle.lower() not in title.lower():
                continue
            self.result.add(
                Fact(
                    "service",
                    "应用身份",
                    {"product": product, "evidence": f"页面标题：{title}"},
                    "title",
                )
            )
            if not path:
                self.result.notes.append(f"应用身份：{product}（来自页面标题），版本见多阶段判断")
                return
            if self._exhausted():
                self.result.notes.append(f"时间盒用尽：未探测 {product} 的版本接口 {path}")
                return
            try:
                resp = self.channel.fetch(urljoin(self.base_url, path))
            except Exception as exc:  # noqa: BLE001 - 取不到版本不是失败，是没证据
                self.result.notes.append(f"{product} 版本接口 {path} 请求失败：{exc}")
                return
            version = _json_version(resp.body) if resp.status == 200 else ""
            if version:
                self.result.add(
                    Fact(
                        "service",
                        f"{product} 版本",
                        {
                            "product": product,
                            "version": version,
                            "evidence": f"{path} → HTTP 200",
                        },
                        "app-version",
                    )
                )
                self.result.notes.append(f"{product} 版本 {version}：来自公开接口 {path}")
            else:
                self.result.notes.append(
                    f"{product} 版本接口 {path} 未给出版本（HTTP {resp.status or '无响应'}）"
                )
            return

    def _step_version_lock(self) -> None:
        self.ledger = _version_ledger(self.result)
        locked = _version_locked(self.ledger)
        policy = _payload_policy(locked)
        self.result.add(
            Fact(
                "service",
                "版本台账",
                {
                    "items": self.ledger,
                    "locked": locked,
                    "payload_policy": policy["policy"],
                    "why": policy["why"],
                },
                "version-lock",
            )
        )
        undetermined = [item["product"] for item in self.ledger if not item["version"]]
        network_only = [
            f"{item['product']}/{item['version']}"
            for item in self.ledger
            if item.get("role") == "network" and item["version"]
        ]
        if network_only and not locked:
            self.result.notes.append(
                "旁路服务版本已知（"
                + "、".join(network_only[:4])
                + "），但 web 应用本身仍未识别 → 仍按未钉死处理，别拿旁路版本替代应用版本"
            )
        if undetermined and not locked:
            self.result.notes.append(
                "以下组件只认出产品、没拿到版本："
                + "、".join(undetermined[:6])
                + "（按低保真处理，别当结论）"
            )

    def _step_entry_face(self) -> None:
        self.entries = _entry_face(self.result)
        self.result.add(
            Fact(
                "surface",
                "入口面",
                {
                    "count": len(self.entries),
                    "entries": self.entries[:20],
                },
                "entry-face",
            )
        )


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _verdict_fact(result: ReconResult) -> dict[str, Any]:
    for fact in result.facts:
        if fact.kind == "note" and fact.title == "难度裁定":
            return fact.data
    return {}


def _print_human(result: ReconResult) -> None:
    from Ben.interface.terminal.text import sanitize_terminal_text as clean

    verdict = _verdict_fact(result)
    print(f"Ben 侦察兵（scout）— {clean(result.base_url)}")
    print(f"通道：{clean(result.channel)}")
    print(f"开始：{result.started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    services = [fact for fact in result.of_kind("service") if "port" in fact.data]
    # 应用版本不是 nmap 给的，而是版本接口问来的；若不标注，目标端口会显示
    # "版本未识别"，与下面台账里的 Juice Shop/20.2.0 自相矛盾。
    app_facts = [fact for fact in result.of_kind("service") if fact.title.endswith(" 版本")]
    app_version = ""
    if app_facts:
        data = app_facts[0].data
        app_version = f"{data.get('product', '')} {data.get('version', '')}".strip()
    target_port = urlsplit(result.base_url).port or (
        443 if result.base_url.startswith("https") else 80
    )
    if services:
        print("服务")
        for fact in sorted(services, key=lambda item: int(item.data["port"])):
            data = fact.data
            version = ""
            if data.get("product") or data.get("version"):
                version = f"{data.get('product', '')} {data.get('version', '')}".strip()
            elif int(data["port"]) == target_port and app_version:
                version = f"{app_version}（应用接口）"
            line = (
                f"  {data['port']:>5}/{data.get('proto', 'tcp'):<4} "
                f"{(data.get('service') or '?'):<10} {version or '版本未识别'}"
            )
            print(clean(line))
    else:
        print("服务   未探测（--no-ports 或工具缺失）")

    ledger = next((fact for fact in result.of_kind("service") if fact.title == "版本台账"), None)
    if ledger:
        data = ledger.data
        print()
        state = "已钉死" if data.get("locked") else "**未钉死**"
        print(clean(f"版本   {state} · 策略 {data.get('payload_policy')}（口径：web 面）"))
        web_items = [item for item in data.get("items", []) if item.get("role", "web") == "web"]
        net_items = [item for item in data.get("items", []) if item.get("role") == "network"]
        for item in web_items[:6]:
            version = item.get("version") or "未知"
            print(clean(f"  · {item['product']} / {version}（{item['confidence']}）"))
        if net_items:
            compact = "、".join(
                f"{item['product']}/{item['version'] or '版本未知'}" for item in net_items[:4]
            )
            print(clean(f"  旁路（不计入 web 判定）：{compact}"))
        if not data.get("locked"):
            print(clean(f"  ↳ {data.get('why', '')}"))

    entries = next((fact for fact in result.of_kind("surface") if fact.title == "入口面"), None)
    if entries:
        print()
        print(f"入口面（{entries.data.get('count', 0)} 条）")
        for item in entries.data.get("entries", [])[:10]:
            status = f" · HTTP {item['status']}" if item.get("status") else ""
            print(clean(f"  · [{item['type']}] {item['where']} — {item['evidence']}{status}"))

    if verdict:
        print()
        print(
            clean(
                f"裁定   {verdict['level'].upper()} · 置信 {verdict['confidence']}"
                f" · easy {verdict['score_easy']} / hard {verdict['score_hard']}"
                f" · 用时 {verdict['elapsed_seconds']}s"
            )
        )
        for signal in verdict.get("signals", [])[:6]:
            arrow = "+" if signal["toward"] == "easy" else "−"
            print(clean(f"  {arrow}{signal['weight']} {signal['signal']}"))
        print(clean(f"  → {verdict['next_step']}"))

    if result.suggestions:
        print()
        print("建议")
        for item in result.suggestions[:6]:
            print(clean(f"  · {item['title']}（{item['why']}）→ {item['next']}"))
    for note in result.notes:
        print()
        print(clean(f"注意   {note}"))
    if result.artifacts:
        print()
        print(clean(f"产物   {result.artifacts.get('dir', '')}"))
        print(
            clean(
                f"  scout.json / scout.md / facts.jsonl / facts.sapper.sh"
                f" · 共 {len(result.facts)} 条事实"
            )
        )


def _render_markdown(result: ReconResult) -> str:
    verdict = _verdict_fact(result)
    lines = [
        f"# 侦察兵报告 — {result.base_url}",
        "",
        f"- 目标：`{result.target}`",
        f"- 主机：`{result.host}`",
        f"- 通道：{result.channel}",
        f"- 时间：{result.started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if verdict:
        lines += [
            f"- 裁定：**{verdict['level']}**（置信 {verdict['confidence']}，"
            f"easy {verdict['score_easy']} / hard {verdict['score_hard']}）",
            f"- 下一步：{verdict['next_step']}",
            f"- 版本：{'已钉死' if verdict.get('version_locked') else '未钉死（禁止盲打 payload）'}",
        ]
    lines += [
        "",
        "> 只读侦察：仅 GET/HEAD 与 TCP 连接探测；未提交表单、未登录、未爆破。",
        "> 难度是**判断**（确定性规则打分），不是观察结论；它只用于排序，不进漏洞报告。",
        "",
    ]
    if verdict:
        lines += ["## 裁定依据", ""]
        for signal in verdict.get("signals", []):
            arrow = "+" if signal["toward"] == "easy" else "−"
            lines.append(f"- {arrow}{signal['weight']} {signal['signal']}")
        lines.append("")
    services = [fact for fact in result.of_kind("service") if "port" in fact.data]
    if services:
        lines += ["## 服务", "", "| 端口 | 服务 | 产品 | 版本 | 证据 |", "|---|---|---|---|---|"]
        for fact in sorted(services, key=lambda item: int(item.data["port"])):
            data = fact.data
            lines.append(
                f"| {data['port']}/{data.get('proto', 'tcp')} | {data.get('service', '')} "
                f"| {data.get('product', '')} | {data.get('version', '')} "
                f"| {data.get('evidence', '')} |"
            )
        lines.append("")
    ledger = next((fact for fact in result.of_kind("service") if fact.title == "版本台账"), None)
    if ledger:
        lines += ["## 版本台账", ""]
        for item in ledger.data.get("items", []):
            lines.append(
                f"- **{item['product']}** / {item['version'] or '未知'}（{item['confidence']}）"
                f" — {', '.join(item['evidence'][:3])}"
            )
        lines += ["", f"策略：`{ledger.data.get('payload_policy')}` — {ledger.data.get('why')}", ""]
    entries = next((fact for fact in result.of_kind("surface") if fact.title == "入口面"), None)
    if entries:
        lines += ["## 入口面", ""]
        for item in entries.data.get("entries", []):
            status = f" · HTTP {item['status']}" if item.get("status") else ""
            lines.append(f"- [{item['type']}] `{item['where']}` — {item['evidence']}{status}")
        lines.append("")
    if result.suggestions:
        lines += ["## 建议下一步", ""]
        for item in result.suggestions:
            lines.append(f"- **{item['title']}** — {item['why']}；{item['next']}")
        lines.append("")
    if result.notes:
        lines += ["## 注意", ""]
        lines += [f"- {note}" for note in result.notes]
        lines.append("")
    return "\n".join(lines)


def _write_artifacts(result: ReconResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "scout.json"
    md_path = out_dir / "scout.md"
    jsonl_path = out_dir / "facts.jsonl"
    sapper_path = out_dir / "facts.sapper.sh"
    payload = result.to_dict()
    payload["verdict"] = _verdict_fact(result)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(result), encoding="utf-8")
    jsonl_path.write_text(
        "\n".join(json.dumps(fact.to_dict(), ensure_ascii=False) for fact in result.facts) + "\n",
        encoding="utf-8",
    )
    sapper_path.write_text(
        "#!/bin/sh\n# 由 `ben scout --emit sapper` 生成：逐条写入 sapper 事实图\n"
        + "\n".join(sapper_commands(result))
        + "\n",
        encoding="utf-8",
    )
    result.artifacts = {
        "dir": str(out_dir),
        "json": str(json_path),
        "markdown": str(md_path),
        "jsonl": str(jsonl_path),
        "sapper": str(sapper_path),
        "raw": str(out_dir / "raw") if (out_dir / "raw").is_dir() else "",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ben scout",
        description="侦察兵：时间盒内出端口/版本/技术栈/入口面，给出 easy|medium|hard 裁定",
        epilog=(
            "退出码：0 完成；1 目标不可达/执行失败；2 目标不在 ALLOWED_TARGETS 内（一个请求都没发）。"
            "只读：GET/HEAD + TCP 连接探测，不提交表单、不登录、不爆破。"
        ),
    )
    parser.add_argument("target", help="目标 URL（必须已写进 .env 的 ALLOWED_TARGETS）")
    parser.add_argument(
        "--minutes",
        type=float,
        default=4.0,
        metavar="MIN",
        help="时间盒（默认 4 分钟，超出即跳过剩余步骤）",
    )
    parser.add_argument(
        "--ports",
        metavar="LIST",
        help=f"端口清单，逗号分隔（默认 {len(_DEFAULT_PORTS)} 个常见端口；传 all 用默认表全集）",
    )
    parser.add_argument("--no-ports", action="store_true", help="跳过端口探测（只看 HTTP 面）")
    parser.add_argument(
        "--port-timeout", type=float, default=2.0, metavar="SECONDS", help="单端口探测超时"
    )
    parser.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS", help="单请求超时")
    parser.add_argument("--json", action="store_true", help="只输出 JSON（含裁定）")
    parser.add_argument(
        "--emit",
        choices=("sapper", "jsonl", "md"),
        help="额外输出：sapper 命令 / 每行一条 fact / markdown",
    )
    parser.add_argument(
        "--out", metavar="DIR", help="产物目录（默认 recon/<host>_<时间戳>_scout/）"
    )
    parser.add_argument("--no-write", action="store_true", help="不落盘，只打印结论")
    parser.add_argument("--no-paths", action="store_true", help="跳过默认路径表探测")
    parser.add_argument("--path-list", metavar="FILE", help="自定义路径清单（每行一个，# 为注释）")
    parser.add_argument(
        "--max-paths",
        type=int,
        default=24,
        metavar="N",
        help="最多探测多少条路径（默认 24；时间盒不够会自动再截断）",
    )
    parser.add_argument("--no-frontend", action="store_true", help="跳过 HTML/JS 提取（不抓脚本）")
    parser.add_argument("--max-js", type=int, default=3, metavar="N", help="最多抓几个同源脚本")
    parser.add_argument("--no-docker", action="store_true", help="只在宿主机侧收集，不启动容器")
    parser.add_argument("--pull", action="store_true", help="镜像缺失时先拉取")
    return parser


def _parse_ports(raw: str | None) -> tuple[int, ...]:
    if not raw or raw.strip().lower() == "all":
        return _DEFAULT_PORTS
    ports: list[int] = []
    for chunk in re.split(r"[,\s]+", raw.strip()):
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            if not (start.isdigit() and end.isdigit()):
                raise ValueError(f"端口区间无法解析：{chunk}")
            low, high = int(start), int(end)
            if low > high or high - low > 1000:
                raise ValueError(f"端口区间不合法：{chunk}")
            ports.extend(range(low, high + 1))
            continue
        if not chunk.isdigit():
            raise ValueError(f"端口无法解析：{chunk}")
        port = int(chunk)
        if not 1 <= port <= 65535:
            raise ValueError(f"端口越界：{port}")
        ports.append(port)
    unique = tuple(dict.fromkeys(ports))
    if not unique:
        raise ValueError("端口清单为空")
    return unique[:_MAX_PORTS_HARD]


def _scout_paths(args: argparse.Namespace) -> tuple[str, ...]:
    """路径表的选择：显式清单 > 侦察兵默认表（入口面优先）；``--no-paths`` 为空。"""
    if args.no_paths:
        return ()
    if args.path_list:
        return _load_paths(args)
    return _SCOUT_PATHS


def run_scout(argv: list[str]) -> int:
    """``ben scout`` 的实现：返回退出码，不抛业务异常。

    顺序与 ``recon`` 一致且同样刻意：分类（不联网）→ 授权（不联网）→ 建容器 →
    预检 → 侦察。授权不过就在**发出第一个请求之前**退出：未授权必须是零网络动作，
    而不是"探了一下再说不行"。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.minutes <= 0:
        parser.error("--minutes 必须大于 0")
    if args.timeout <= 0 or args.port_timeout <= 0:
        parser.error("--timeout / --port-timeout 必须大于 0")
    if args.max_paths < 0 or args.max_js < 0:
        parser.error("--max-paths / --max-js 不能为负")
    try:
        ports = _parse_ports(args.ports)
    except ValueError as exc:
        parser.error(str(exc))

    from Ben.config import load_settings

    try:
        settings = load_settings(require_api_key=False)
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要给出可读结论
        print(f"[FAIL] 读取配置失败：{exc}")
        return 1
    allowed = tuple(getattr(settings, "allowed_targets", ()) or ())

    try:
        paths = _scout_paths(args)
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
        print(f"[FAIL] scout 只支持 web/ip 目标，当前是 {kind or '未知'}")
        return 1

    host = _host_of(args.target) or str((infos[0].get("details") or {}).get("target_ip") or "")
    authorized, auth_detail = _authorized(host, allowed)
    if not authorized:
        print(f"[FAIL] {args.target} 不在授权范围，未发出任何请求")
        print(f"       {auth_detail}")
        print("       把它加进 .env 的 ALLOWED_TARGETS 后重试")
        return 2

    # 交互模式才打进度：--json / --emit 的输出是要被程序消费的，混进进度行会破坏解析。
    interactive = not args.json and not args.emit
    progress = _Progress() if interactive else None
    if progress is not None:
        print(f"Ben 侦察兵（scout）— {args.target}", flush=True)
        print(
            f"时间盒 {args.minutes:g} 分钟 · 端口表 {len(ports)} 个"
            f"{'（--no-ports）' if args.no_ports else ''}"
            f" · 路径表 {len(paths)} 条 · 通道 {'宿主机' if args.no_docker else '容器'}",
            flush=True,
        )

    engine: _ContainerEngine | None = None
    docker_probe = None
    if not args.no_docker:
        if progress is not None:
            progress.step("准备容器视角（拉取/启动沙箱，首次可能要几十秒）")
        engine = _ContainerEngine(settings, pull=args.pull, role="scout")
        docker_probe = engine.prepare()
        if docker_probe.status != "ok":
            print(f"[WARN] 容器视角不可用：{docker_probe.detail}")
            print("       退回宿主机视角；结论只代表本机网络位置，不等于扫描端可达")
            engine = None
        elif progress is not None:
            progress.step(f"容器就绪：{docker_probe.detail}")

    try:
        if progress is not None:
            progress.step("预检：目标可达性 + 授权复核")
        report = check_target(
            args.target,
            allowed=allowed,
            timeout=args.timeout,
            engine=engine,
            container_skip_reason="已指定 --no-docker" if args.no_docker else "容器视角不可用",
        )
        if docker_probe is not None:
            report.probes.insert(0, docker_probe)
        return _scout_with_report(
            args,
            report=report,
            engine=engine,
            allowed=allowed,
            paths=paths,
            ports=ports,
            progress=progress,
        )
    finally:
        if engine is not None:
            engine.close()


def _scout_with_report(
    args: argparse.Namespace,
    *,
    report: Any,
    engine: Any,
    allowed: tuple[str, ...],
    paths: tuple[str, ...],
    ports: tuple[int, ...],
    progress: _Progress | None = None,
) -> int:
    from Ben.interface.terminal.text import sanitize_terminal_text as clean

    if not report.authorized:
        print(clean(f"[FAIL] {report.target} 不在授权范围，未发出任何请求"))
        for probe in report.probes:
            if probe.name == "authorize":
                print(clean(f"       {probe.detail}"))
        print("       把它加进 .env 的 ALLOWED_TARGETS 后重试")
        return 2
    if not report.reachable:
        print(clean(f"[FAIL] {report.target} 不可达，侦察未开始"))
        for probe in report.probes:
            if probe.name in ("probe-host", "probe-container", "probe-ip"):
                print(clean(f"       {probe.name}: {probe.detail}"))
                if probe.hint:
                    print(clean(f"       ↳ {probe.hint}"))
        print("       先确认服务已启动：ben preflight <target>")
        return 1
    if report.kind not in ("web_application", "ip_address"):
        print(clean(f"[FAIL] scout 只支持 web/ip 目标，当前是 {report.kind or '未知'}"))
        return 1

    channel = _Channel(engine, allowed=allowed, timeout=args.timeout)
    base_url = report.url
    if not channel.in_container:
        from Ben.interface.cli.target_check import _host_view_url

        base_url = _host_view_url(report.target, report.url)
    host = (urlsplit(base_url).hostname or "").lower()

    out_dir = (
        Path(args.out)
        if args.out
        else Path("recon") / (f"{_slug(host)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_scout")
    )
    raw_dir = None if args.no_write else out_dir / "raw"
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)

    scout = Scout(
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
        minutes=args.minutes,
        ports=ports,
        port_scan=not args.no_ports,
        port_timeout=args.port_timeout,
    )
    scout.progress = progress
    try:
        result = scout.run()
    except GateError as exc:
        print(clean(f"[FAIL] 命令未通过授权/只读闸门：{exc}"))
        return 1

    if not args.no_write:
        _write_artifacts(result, out_dir)

    if args.json:
        payload = result.to_dict()
        payload["verdict"] = _verdict_fact(result)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.emit == "sapper":
        print("\n".join(sapper_commands(result)))
        return 0
    if args.emit == "jsonl":
        print("\n".join(json.dumps(fact.to_dict(), ensure_ascii=False) for fact in result.facts))
        return 0
    if args.emit == "md":
        print(_render_markdown(result))
        return 0
    _print_human(result)
    return 0
