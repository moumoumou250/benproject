"""``ben vuln`` 子命令：聚焦单类漏洞的入口（A 档：不写编排代码）。

本模块只做三件事：

1. 把用户写的漏洞类型（``sqli`` / ``xss,csrf``）解析成 ``Ben/skills`` 里的
   ``vulnerabilities/<name>`` 技能限定名；
2. **启动前**校验它们真的存在（见下方"为什么必须前置校验"）；
3. 把一段"聚焦指令"拼到用户 instruction 前面。

之后整份 args 原样交给 :func:`Ben.interface.main._run_task`，与普通扫描走同
一条 `validate_environment → pull_docker_image → check_run_targets →
prepare_run → run_cli` 路径。因此本模块**不碰** `Ben/core`、`Ben/agents`、
`Ben/tools`：授权闸门、预检、沙箱、报告、run 记录、`--fail-on` 退出码全部复用，
差异只体现在提示词里。

编排与调试闸门
--------------
root agent 是纯编排者（``Ben/skills/coordination/root_agent.md``），子 agent
由它经 ``create_agent(skills=[...])`` 拉起。聚焦指令仍写在 prompt 里；此外
**代码强制** ``BEN_MAX_SUBAGENTS=1``（防模型偷开多个子 agent）。指定漏洞回
归用 ``--expect PATTERN``：跑完对照 ``vulnerabilities.json``，未命中退出码
3；``--fail-on`` 仍是「有洞失败」（退出码 2），极性相反。

为什么必须前置校验
------------------
``Ben/agents/prompt.py:_resolve_skills`` 对未注册技能是 **warn-and-skip**：
名字写错时扫描照跑，只是没加载任何方法论文本，日志里一行 warning 而已。
聚焦模式如果撞上这个行为，用户会得到一次"看起来在测 SQLi、实际没有任何
SQLi 方法论"的扫描。所以这里用 ``resolve_skill_name`` 硬校验，失败即退出。

典型用法
--------
::

    python main.py vuln --list
    python main.py vuln sqli -t http://target.example:8080/
    python main.py vuln xss,csrf -t http://target.example:8080/ --dry-run
    python main.py vuln idor -t http://target.example:8080/ --max-turns 8 --max-budget 0.3
    python main.py vuln idor -t http://target/ --expect 'IDOR' --expect '/api/users' --max-turns 10
"""

from __future__ import annotations

import argparse
import difflib
import re
from typing import Any
from collections.abc import Callable

__all__ = ["build_parser", "resolve_vulnerability_skills", "run_vuln"]

# create_agent / load_skill 的上限（Ben/tools/load_skill/tool.py: MAX_SKILLS_PER_CALL）。
MAX_SKILLS = 5

# 常见的缩写写法 → Ben/skills/vulnerabilities 里的规范名。
# 只收"人真的会打"的别名；其余靠规范化 + 前缀/近似匹配兜底，
# 避免把 28 个技能名抄两遍后忘了同步。
_ALIASES: dict[str, str] = {
    "sqli": "sql-injection",
    "sqlinject": "sql-injection",
    "blind-sqli": "sql-injection",
    "nosqli": "nosql-injection",
    "bola": "idor",
    "bfla": "broken-function-level-authorization",
    "broken-access-control": "idor",
    "access-control": "idor",
    "lfi": "path-traversal-lfi-rfi",
    "rfi": "path-traversal-lfi-rfi",
    "traversal": "path-traversal-lfi-rfi",
    "path-traversal": "path-traversal-lfi-rfi",
    "file-read": "path-traversal-lfi-rfi",
    "crlf": "header-injection",
    "response-splitting": "header-injection",
    "race": "race-conditions",
    "race-condition": "race-conditions",
    "jwt": "authentication-jwt",
    "auth": "authentication-jwt",
    "authentication": "authentication-jwt",
    "logic": "business-logic",
    "biz-logic": "business-logic",
    "infoleak": "information-disclosure",
    "info-disclosure": "information-disclosure",
    "takeover": "subdomain-takeover",
    "subdomain": "subdomain-takeover",
    "weak-password": "weak-password-detection",
    "bruteforce": "weak-password-detection",
    "default-creds": "weak-password-detection",
    "upload": "insecure-file-uploads",
    "file-upload": "insecure-file-uploads",
    "deserialization": "insecure-deserialization",
    "smuggling": "http-request-smuggling",
    "request-smuggling": "http-request-smuggling",
    "llm": "llm-prompt-injection",
    "prompt-injection": "llm-prompt-injection",
    "dom": "browser-security",
    "client-side": "browser-security",
    "agentic": "agentic-system-security",
    "mcp": "agentic-system-security",
    "ai-agent": "agentic-system-security",
    "openredirect": "open-redirect",
    "redirect": "open-redirect",
    "massassign": "mass-assignment",
    "prototype": "prototype-pollution",
}

# 聚焦指令：插在用户 instruction 之前。措辞对齐 system_prompt 的既有纪律
# （证据分级、不推测、上报格式），只收紧"范围"和"委派"两件事。
_FOCUS_TEMPLATE = """\
<focused_scan types="{types}" skills="{skills}">
本次 run 是**聚焦模式**：只测上面这几类问题，不做全量侦察，也不做全类型铺开。

- 只创建一个子 agent（create_agent），它的 `skills` 参数必须**正好**是上面列出的
  技能名；技能挂在子 agent 上，不要挂在自己（root）身上——你不做探测。
- 子任务范围只到"确认该类问题在授权目标上是否真实存在"，不做无关的目录/参数/
  子域枚举。侦察严格够用即止。
- 需要请求体的探测（登录表单、JSON 接口）优先用代理工具的 repeat_request。没有
  代理可用时不要写 `curl -d`、`sqlmap --data`、`ffuf -X POST` 这类命令——只读闸门
  会直接拒绝，纯属浪费轮数；GET 参数、路径、请求头注入类不受此限。
- 证据不足就报「未发现」，不写推测性结论；确认发现时给可复现步骤与原始响应。
- 子 agent 返回的每条确认发现，由你**立即**用 create_vulnerability_report 登记
  （子 agent 不写最终报告）；只写在总结里等于没发现。
- 该类型测完就 finish_scan，不要顺手补测其他类型。
</focused_scan>"""


class VulnTypeError(ValueError):
    """漏洞类型无法解析；消息面向用户，含可用清单/近似建议。"""


def _normalize(raw: str) -> str:
    """把用户写法收敛成 kebab-case：``SQL_Injection`` → ``sql-injection``。"""
    key = raw.strip().lower().replace("_", "-").replace(" ", "-")
    return re.sub(r"-{2,}", "-", key).strip("-")


def _vulnerability_catalog() -> dict[str, str]:
    """``{技能名: 描述}``，只取 ``vulnerabilities`` 分类。"""
    from Ben.skills import skill_catalog

    entries = skill_catalog().get("vulnerabilities", [])
    return {str(item["name"]): str(item.get("description") or "") for item in entries}


def resolve_vulnerability_skills(
    raw_types: list[str], extra_skills: list[str] | None = None
) -> list[str]:
    """把用户写的类型解析成 ``category/name`` 限定名列表。

    Raises:
        VulnTypeError: 类型无法解析、或技能总数超过 :data:`MAX_SKILLS`。
    """
    from Ben.skills import SkillError, resolve_skill_name, validate_requested_skills

    catalog = _vulnerability_catalog()
    if not catalog:
        raise VulnTypeError("没有发现任何 vulnerabilities 技能，检查 Ben/skills/ 是否完整")

    resolved: list[str] = []
    unknown: list[str] = []
    for raw in raw_types:
        for piece in str(raw).split(","):
            key = _normalize(piece)
            if not key:
                continue
            candidate = _ALIASES.get(key, key)
            # 先查本地目录再 resolve：未命中的名字不该在 stderr 留一行
            # "请求了未注册片段" 的 warning（用户看到的是下面的建议清单）。
            if candidate not in catalog:
                unknown.append(piece.strip())
                continue
            qualified = resolve_skill_name(f"vulnerabilities/{candidate}")
            if qualified not in resolved:
                resolved.append(qualified)

    if unknown:
        hints: list[str] = []
        for piece in unknown:
            close = difflib.get_close_matches(_normalize(piece), sorted(catalog), n=3, cutoff=0.5)
            hints.append(f"  · {piece!r}" + (f" —— 是不是 {', '.join(close)}？" if close else ""))
        raise VulnTypeError(
            "无法识别的漏洞类型：\n"
            + "\n".join(hints)
            + f"\n可用类型（{len(catalog)} 个）：{', '.join(sorted(catalog))}"
            + "\n用 `python main.py vuln --list` 看描述。"
        )

    for extra in extra_skills or []:
        try:
            qualified = resolve_skill_name(extra)
        except SkillError as exc:
            raise VulnTypeError(f"附加技能无法解析：{exc}") from exc
        if qualified not in resolved:
            resolved.append(qualified)

    error = validate_requested_skills(resolved, max_skills=MAX_SKILLS)
    if error:
        raise VulnTypeError(
            f"技能过多：{error}\n（聚焦模式最多 {MAX_SKILLS} 个技能，拆成多次扫描）"
        )
    return resolved


def _focus_block(types: list[str], skills: list[str]) -> str:
    """生成聚焦指令块（纯文本，进 instruction，因此随 run.json 一起持久化）。"""
    short_types = ", ".join(s.split("/", 1)[-1] for s in types)
    return _FOCUS_TEMPLATE.format(types=short_types, skills=", ".join(skills))


def _print_catalog() -> None:
    """``--list``：打印可用漏洞类型（不需要 .env，不需要 Docker）。"""
    catalog = _vulnerability_catalog()
    if not catalog:
        print("[FAIL] 没有发现 vulnerabilities 技能")
        return
    reverse: dict[str, list[str]] = {}
    for alias, target in _ALIASES.items():
        reverse.setdefault(target, []).append(alias)

    width = max(len(name) for name in catalog)
    print(f"可用漏洞类型（{len(catalog)} 个，来自 Ben/skills/vulnerabilities/）：\n")
    for name in sorted(catalog):
        description = catalog[name].strip().split("\n")[0]
        if len(description) > 88:
            description = description[:87].rstrip() + "…"
        aliases = ", ".join(sorted(reverse.get(name, [])))
        print(f"  {name:<{width}}  {description}")
        if aliases:
            print(f"  {'':<{width}}  别名：{aliases}")
    print("\n用法：python main.py vuln <类型> -t <授权目标>")
    print("多个类型用逗号分隔（最多 5 个技能）：python main.py vuln xss,csrf -t http://t/")


def build_parser() -> argparse.ArgumentParser:
    """``vuln`` 子命令的参数（与主命令同名参数保持一致的手感）。"""
    parser = argparse.ArgumentParser(
        prog="ben vuln",
        description="聚焦单类漏洞的扫描：只加载对应技能，开一个子 agent，不做全量侦察。",
    )
    parser.add_argument(
        "vuln_type", nargs="?", default="", help="漏洞类型，如 sqli / xss / idor；逗号分隔可多个"
    )
    parser.add_argument(
        "-t",
        "--target",
        dest="targets",
        action="append",
        metavar="TARGET",
        help="测试目标（URL/域名/IP/本地目录）。可重复。",
    )
    parser.add_argument(
        "--target-list",
        action="append",
        metavar="PATH",
        help="目标清单文件，每行一个，忽略空行与 # 注释。",
    )
    parser.add_argument(
        "--skills",
        action="append",
        metavar="NAME",
        help="额外挂载的技能（如 tooling/sqlmap）。可重复，总数含漏洞技能不超过 5。",
    )
    parser.add_argument("--instruction", help="附加指令（会拼在聚焦指令之后）。")
    parser.add_argument(
        "-m",
        "--scan-mode",
        choices=["quick", "standard", "deep"],
        default="quick",
        help="扫描模式片段，聚焦模式默认 quick。",
    )
    parser.add_argument("--max-turns", type=int, metavar="N", help="每个 agent 的最大轮数。")
    parser.add_argument(
        "--max-budget",
        "--max-budget-usd",
        dest="max_budget_usd",
        type=float,
        metavar="USD",
        help="本次扫描的 LLM 成本上限（美元）。",
    )
    parser.add_argument(
        "--fail-on",
        type=str.lower,
        choices=["critical", "high", "medium", "low", "info"],
        metavar="SEVERITY",
        help="发现该等级及以上漏洞时以退出码 2 结束（CI：有洞失败）。",
    )
    parser.add_argument(
        "--expect",
        action="append",
        metavar="PATTERN",
        help=(
            "检出回归：vulnerabilities.json 中须至少一条命中该子串"
            "（匹配 title/endpoint/cwe/…，可重复或逗号分隔）。"
            "全部命中才算过；未命中退出码 3。与 --fail-on 极性相反。"
        ),
    )
    parser.add_argument(
        "--workspace-file",
        action="append",
        metavar="PATH[:DEST]",
        help="扫描前把本机文件放进沙箱 /workspace。",
    )
    parser.add_argument(
        "--seed",
        metavar="PATH",
        help="注入 recon/scout 先验产物（同主命令 --seed）。",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="禁止自动注入 recon/scout 种子。",
    )
    parser.add_argument("--skip-preflight", action="store_true", help="跳过目标可达性预检。")
    parser.add_argument(
        "--preflight-timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="预检单次探测超时（默认 5 秒）。",
    )
    parser.add_argument("--keep-container", action="store_true", help="任务结束保留执行容器。")
    parser.add_argument("--mcp-config", metavar="PATH", help="使用指定 MCP servers JSON。")
    parser.add_argument(
        "--mcp-server",
        dest="mcp_server",
        action="append",
        metavar="NAME",
        help="只启用指定名称的 MCP 连接。",
    )
    parser.add_argument(
        "--mcp-exclude",
        dest="mcp_exclude",
        action="append",
        metavar="NAME",
        help="跳过指定名称的 MCP 连接。",
    )
    parser.add_argument("--config", metavar="PATH", help="使用指定 cli-config.json。")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印解析出的技能与聚焦指令，不启动扫描（不需要模型/Docker）。",
    )
    parser.add_argument("--list", action="store_true", help="列出可用漏洞类型后退出。")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser


def _collect_targets(
    ns: argparse.Namespace, parser: argparse.ArgumentParser
) -> list[dict[str, Any]]:
    """合并 ``-t`` 与 ``--target-list``，交给共享分类器（与主入口同一套校验）。"""
    from Ben.interface.shared.targets import build_targets_info, read_target_list_file

    targets: list[str] = list(ns.targets or [])
    for path in ns.target_list or []:
        try:
            targets.extend(read_target_list_file(path))
        except ValueError as exc:
            parser.error(str(exc))
    if not targets:
        return []
    try:
        return build_targets_info(targets)
    except ValueError as exc:
        parser.error(str(exc))
        return []


def _build_run_args(
    ns: argparse.Namespace, parser: argparse.ArgumentParser, instruction: str
) -> argparse.Namespace:
    """拼出与 :func:`Ben.interface.cli.arguments.parse_arguments` 同构的 args。

    ``_run_task`` / ``run_cli`` / ``prepare_run`` 只通过 ``getattr`` 读这些字段，
    字段齐了就能直接复用整条链路（含 fail_on 与 run 记录）。
    """
    targets_info = _collect_targets(ns, parser)
    from Ben.interface.cli.debug_expect import normalize_expect_patterns

    args = argparse.Namespace(
        target="",
        targets=None,
        target_list=None,
        instruction=instruction,
        instruction_file=None,
        scan_mode=ns.scan_mode,
        scope_mode="auto",
        diff_base=None,
        resume=None,
        resume_picker=False,
        interactive=False,
        non_interactive=True,
        workspace_file=ns.workspace_file,
        workspace_files=[],
        seed=getattr(ns, "seed", None),
        no_seed=bool(getattr(ns, "no_seed", False)),
        max_budget_usd=ns.max_budget_usd,
        max_turns=ns.max_turns,
        fail_on=ns.fail_on,
        expect=normalize_expect_patterns(getattr(ns, "expect", None)),
        debug_summary=True,  # vuln 调试入口默认打印跑完摘要
        config=ns.config,
        mcp_config=ns.mcp_config,
        mcp_server=ns.mcp_server,
        mcp_exclude=ns.mcp_exclude,
        selftest_docker=False,
        selftest_llm=False,
        selftest_tools=False,
        skip_preflight=ns.skip_preflight,
        preflight_timeout=ns.preflight_timeout,
        keep_container=ns.keep_container,
        verbose=ns.verbose,
        # parse 阶段产出的运行输入
        targets_info=targets_info,
        local_sources=[],
        diff_scope={"active": False},
        needs_setup=False,
        user_explicit_instruction=None,
        workspace_mount=None,
        run_name=None,
    )
    if ns.workspace_file:
        from Ben.interface.shared.targets import resolve_workspace_files

        try:
            args.workspace_files = resolve_workspace_files(ns.workspace_file)
        except ValueError as exc:
            parser.error(str(exc))
    return args


def run_vuln(argv: list[str], run_task: Callable[[Any], int] | None = None) -> int:
    """``ben vuln`` 的实现：返回退出码，不抛业务异常。

    执行顺序是刻意的：**先解析技能（不联网、不读 .env），再交给主链路**。
    类型写错时立刻退出，不创建 run、不拉镜像、不花额度。
    """
    parser = build_parser()
    ns = parser.parse_args(argv)

    if ns.list:
        _print_catalog()
        return 0
    if ns.max_turns is not None and ns.max_turns < 1:
        parser.error("--max-turns 必须是正整数")
    if ns.max_budget_usd is not None and ns.max_budget_usd <= 0:
        parser.error("--max-budget 必须大于 0")
    if ns.preflight_timeout <= 0:
        parser.error("--preflight-timeout 必须大于 0")

    if not ns.vuln_type:
        parser.error("缺少漏洞类型。先运行 `python main.py vuln --list` 看可用类型")

    try:
        skills = resolve_vulnerability_skills([ns.vuln_type], ns.skills)
    except VulnTypeError as exc:
        print(f"[FAIL] {exc}")
        return 2

    types = [s for s in skills if s.startswith("vulnerabilities/")]
    block = _focus_block(types, skills)
    instruction = f"{block}\n\n{ns.instruction}" if ns.instruction else block

    try:
        args = _build_run_args(ns, parser, instruction)
    except SystemExit as exc:  # parser.error 走这里：目标非法
        return int(exc.code or 2)

    if ns.dry_run:
        print("[DRY-RUN] 未启动扫描，以下是将要注入的内容：\n")
        print(f"漏洞类型：{', '.join(t.split('/', 1)[-1] for t in types) or '（仅附加技能）'}")
        print(f"挂载技能：{', '.join(skills)}")
        print(f"目标：{[t.get('original') for t in args.targets_info] or '（.env 白名单首项）'}")
        print(
            f"扫描模式：{args.scan_mode} · 轮数：{args.max_turns or '默认'} · 预算：{args.max_budget_usd or '不限'}"
        )
        print("子 agent 上限：BEN_MAX_SUBAGENTS=1（vuln 强制，防发散）")
        if args.expect:
            print(f"expect：{args.expect}（未全部命中 → exit 3）")
        print("\n--- instruction ---")
        print(instruction)
        return 0

    if ns.config:
        from pathlib import Path

        from Ben.config.loader import apply_config_override

        apply_config_override(Path(ns.config))

    import os

    # 聚焦调试：代码强制单子 agent（提示词约束不够硬）。
    os.environ["BEN_MAX_SUBAGENTS"] = "1"
    if ns.mcp_config:
        os.environ["BEN_MCP_CONFIG"] = str(ns.mcp_config)
    if ns.mcp_server:
        os.environ["BEN_MCP_ONLY"] = ",".join(ns.mcp_server)
    if ns.mcp_exclude:
        os.environ["BEN_MCP_EXCLUDE"] = ",".join(ns.mcp_exclude)

    if run_task is None:  # 兜底：直接以模块方式调用时也能工作
        from Ben.interface.main import _run_task as run_task  # noqa: PLC0415

    try:
        return run_task(args)
    except KeyboardInterrupt:
        return 130
