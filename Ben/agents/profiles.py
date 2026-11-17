"""按需 agent 工具面（Profile）与短命派生规格（SpawnSpec）。

记忆在账本，不在 agent：Profile 定义「挂哪些工具 / shell 允哪些二进制」；
ToolkitCache 按 ``(sandbox_session, profile)`` 复用已装配工具面，避免每个
DFS 节点付全额冷启动；SpawnSpec 描述一次 ephemeral worker 的任务载荷。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 受 profile shell_allow 门控的二进制（网络客户端 / 扫描器族）。
# 未列入的本地助手（cat/jq/…）不拦；主机白名单闸门仍管网络目标。
_PROFILE_GATED_BINS: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "http",
        "https",
        "nc",
        "ncat",
        "netcat",
        "socat",
        "nmap",
        "masscan",
        "naabu",
        "ping",
        "ping6",
        "dig",
        "nslookup",
        "host",
        "whois",
        "traceroute",
        "ssh",
        "scp",
        "sftp",
        "telnet",
        "ftp",
        "smbclient",
        "nikto",
        "sqlmap",
        "ffuf",
        "gobuster",
        "dirb",
        "nuclei",
        "wfuzz",
        "httpx",
        "openssl",
        "katana",
    }
)

_VALID_PROFILE_NAMES: frozenset[str] = frozenset(
    {"recon", "enum", "exploit", "validate", "sca", "default"}
)

# 报告写工具 / 威胁建模写工具：供 factory 的 report_tool 判定与 root drop 复用。
REPORT_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_vulnerability_report",
        "create_dependency_report",
        "update_vulnerability_report",
        "delete_vulnerability_report",
    }
)


@dataclass(frozen=True)
class ProfileSpec:
    """静态工具面规格。

    - ``tool_names is None``：挂全量 child 工具，再减去 ``tool_drop``（兼容 default）。
    - ``tool_names`` 有值：只挂点名工具（recon/enum）；``tool_drop`` 忽略。
    """

    name: str
    tool_names: frozenset[str] | None
    shell_allow: frozenset[str] | None
    skills: tuple[str, ...] = ()
    max_turns: int | None = None
    batch_ok: bool = False
    requires_structured_exit: bool = False
    include_filesystem: bool = True
    tool_drop: frozenset[str] = frozenset()


# 对齐 docs/dfs-on-demand-design-v1.md §3；端口/目录差异用 task + shell_allow，
# 不拆 RECON_PORT/DIR 两个 profile。
PROFILES: dict[str, ProfileSpec] = {
    "recon": ProfileSpec(
        name="recon",
        tool_names=frozenset(
            {
                "exec_command",
                "write_stdin",
                "think",
                "record_coverage",
                "list_coverage",
                "record_fact",
                "list_facts",
                "agent_finish",
            }
        ),
        shell_allow=frozenset(
            {
                "nmap",
                "naabu",
                "masscan",
                "ffuf",
                "gobuster",
                "curl",
                "wget",
                "httpx",
                "nc",
                "dig",
                "host",
            }
        ),
        skills=("reconnaissance",),
        max_turns=8,
        batch_ok=True,
        requires_structured_exit=True,
        include_filesystem=False,
    ),
    "enum": ProfileSpec(
        name="enum",
        tool_names=frozenset(
            {
                "exec_command",
                "write_stdin",
                "think",
                "load_skill",
                "record_coverage",
                "list_coverage",
                "record_fact",
                "list_facts",
                "list_sitemap",
                "view_sitemap_entry",
                "agent_finish",
            }
        ),
        shell_allow=frozenset(
            {
                "ffuf",
                "gobuster",
                "dirb",
                "curl",
                "wget",
                "httpx",
                "katana",
                "nmap",
            }
        ),
        skills=("reconnaissance",),
        max_turns=12,
        batch_ok=True,
        requires_structured_exit=True,
        include_filesystem=False,
    ),
    # R1/T1：执行类从全量面减挂，避免为白名单重抄整份 _BASE_TOOLS。
    # exploit：动态漏洞 create/update；无 SCA create、无 delete；威胁只读+amend。
    "exploit": ProfileSpec(
        name="exploit",
        tool_names=None,
        shell_allow=None,
        batch_ok=False,
        requires_structured_exit=False,
        include_filesystem=True,
        tool_drop=frozenset(
            {
                "create_dependency_report",
                "delete_vulnerability_report",
                "save_threat_model",
            }
        ),
    ),
    # validate：修订/盘点/撤回；不新建 vuln/dep；威胁只读+amend。
    "validate": ProfileSpec(
        name="validate",
        tool_names=None,
        shell_allow=None,
        batch_ok=False,
        requires_structured_exit=False,
        include_filesystem=True,
        tool_drop=frozenset(
            {
                "create_vulnerability_report",
                "create_dependency_report",
                "save_threat_model",
            }
        ),
    ),
    # sca：已知 CVE 依赖；无动态 create；威胁只读+amend。
    "sca": ProfileSpec(
        name="sca",
        tool_names=None,
        shell_allow=None,
        batch_ok=False,
        requires_structured_exit=False,
        include_filesystem=True,
        tool_drop=frozenset(
            {
                "create_vulnerability_report",
                "delete_vulnerability_report",
                "save_threat_model",
            }
        ),
    ),
    # default / profile=None：保留全量报告面（兼容旧 spawn）；威胁仍禁 save（T1）。
    "default": ProfileSpec(
        name="default",
        tool_names=None,
        shell_allow=None,
        batch_ok=False,
        requires_structured_exit=False,
        include_filesystem=True,
        tool_drop=frozenset({"save_threat_model"}),
    ),
}


def normalize_profile(profile: str | None) -> str | None:
    """``None`` / 空 / ``default`` → None（全量兼容路径）；非法名抛 ValueError。"""
    if profile is None:
        return None
    name = str(profile).strip().lower()
    if not name or name == "default":
        return None
    if name not in _VALID_PROFILE_NAMES:
        raise ValueError(
            f"unknown agent profile {profile!r}; "
            f"expected one of {sorted(_VALID_PROFILE_NAMES)}"
        )
    return name


def get_profile(profile: str | None) -> ProfileSpec:
    key = normalize_profile(profile)
    if key is None:
        return PROFILES["default"]
    return PROFILES[key]


@dataclass
class SpawnSpec:
    """一次 ephemeral 派生的任务载荷（可先于完整 task_graph 使用）。"""

    profile: str
    task_payload: str
    intent_id: str | None = None
    surface: str | None = None
    risk_area: str | None = None
    budget_turns: int | None = None
    batch_intent_ids: tuple[str, ...] = ()

    def resolved_profile(self) -> ProfileSpec:
        return get_profile(self.profile)


_TASK_TEMPLATES: dict[str, str] = {
    "recon": (
        "<ephemeral_profile profile=\"recon\">\n"
        "You are a short-lived recon worker. Do ONE work unit, then finish.\n"
        "1) Run the scoped scanner via exec_command (nmap/ffuf/… only).\n"
        "2) Persist every finding with record_fact and/or record_coverage "
        "BEFORE agent_finish.\n"
        "3) Call agent_finish with result_json summarizing ports[] or paths[], "
        "or ruled_out_json if nothing new.\n"
        "Do not spawn children. Do not keep exploring beyond this brief.\n"
        "</ephemeral_profile>\n\n"
        "{task}"
    ),
    "enum": (
        "<ephemeral_profile profile=\"enum\">\n"
        "You are a short-lived enumeration worker. Finish after this batch.\n"
        "1) Enumerate the scoped surface (ffuf/gobuster/sitemap tools).\n"
        "2) record_fact / record_coverage for each new path or endpoint.\n"
        "3) agent_finish with result_json paths[]/endpoints[] or ruled_out_json.\n"
        "Do not spawn children.\n"
        "</ephemeral_profile>\n\n"
        "{task}"
    ),
}


def render_ephemeral_task(profile: str | None, task: str) -> str:
    """给 recon/enum 任务套上短命完成契约模板；其它 profile 原样返回。"""
    key = normalize_profile(profile)
    if key is None or key not in _TASK_TEMPLATES:
        return task
    body = (task or "").strip()
    rendered = _TASK_TEMPLATES[key].format(task=body)
    if len(rendered) > 4096:
        # 硬上限：模板 + 截断后的 task，避免首包膨胀。
        budget = 4096 - (len(rendered) - len(body))
        truncated = body[: max(0, budget - 20)].rstrip() + "\n…[truncated]"
        rendered = _TASK_TEMPLATES[key].format(task=truncated)
    return rendered


@dataclass
class CachedToolkit:
    profile: str
    tools: list[Any]
    tool_names: tuple[str, ...] = field(default_factory=tuple)


class ToolkitCache:
    """按 ``(id(sandbox_session), profile)`` 缓存已包装工具列表。

    Agent 实例仍每次新建；复用的是工具面装配成本。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[int, str], CachedToolkit] = {}

    def get(self, sandbox_session: Any, profile: str) -> CachedToolkit | None:
        return self._cache.get((id(sandbox_session), profile))

    def put(self, sandbox_session: Any, profile: str, cached: CachedToolkit) -> CachedToolkit:
        self._cache[(id(sandbox_session), profile)] = cached
        return cached

    def get_or_build(
        self,
        sandbox_session: Any,
        profile: str,
        builder: Any,
    ) -> CachedToolkit:
        key = (id(sandbox_session), profile)
        hit = self._cache.get(key)
        if hit is not None:
            logger.debug("toolkit cache hit profile=%s", profile)
            return hit
        built = builder()
        if not isinstance(built, CachedToolkit):
            names = tuple(getattr(t, "name", "?") for t in built)
            built = CachedToolkit(profile=profile, tools=list(built), tool_names=names)
        self._cache[key] = built
        logger.info(
            "toolkit cache miss → built profile=%s tools=%d",
            profile,
            len(built.tools),
        )
        return built

    def prefetch(
        self,
        sandbox_session: Any,
        profiles: tuple[str, ...],
        builder_for: Any,
    ) -> None:
        for name in profiles:
            key = normalize_profile(name) or "default"
            self.get_or_build(sandbox_session, key, lambda n=key: builder_for(n))

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


# 进程级默认缓存；runner 可换实例，测试可 clear。
_default_toolkit_cache = ToolkitCache()


def get_toolkit_cache() -> ToolkitCache:
    return _default_toolkit_cache


def reset_toolkit_cache() -> None:
    _default_toolkit_cache.clear()


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*")


def command_binaries(command: str) -> set[str]:
    """从命令各简单段提取 argv0 基名（小写）。"""
    from Ben.agents.factory import _simple_commands

    names: set[str] = set()
    for segment in _simple_commands(command):
        tokens = [tok for tok in re.split(r"\s+", segment.strip()) if tok]
        i = 0
        while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
            i += 1
        if i >= len(tokens):
            continue
        raw = tokens[i].strip("\"'")
        base = raw.rsplit("/", 1)[-1].lower()
        if base in {"sudo", "command", "env"} and i + 1 < len(tokens):
            nxt = tokens[i + 1].strip("\"'")
            if not nxt.startswith("-"):
                base = nxt.rsplit("/", 1)[-1].lower()
        if base:
            names.add(base)
    return names


def check_profile_shell_allow(
    command: str, shell_allow: frozenset[str] | None
) -> str | None:
    """profile 正向前置：门控族二进制必须在 shell_allow 内。None allow = 不额外收窄。"""
    if shell_allow is None:
        return None
    denied: list[str] = []
    for binary in sorted(command_binaries(command)):
        if binary in _PROFILE_GATED_BINS and binary not in shell_allow:
            denied.append(binary)
    if not denied:
        return None
    return (
        f"profile shell allowlist denies {denied}; "
        f"allowed scanners: {sorted(shell_allow)}"
    )


def mark_structured_exit(context: dict[str, Any] | None, *, source: str) -> None:
    if not isinstance(context, dict):
        return
    context["structured_exit_ok"] = True
    context["structured_exit_source"] = source


def has_structured_exit(context: dict[str, Any] | None) -> bool:
    return bool(isinstance(context, dict) and context.get("structured_exit_ok"))
