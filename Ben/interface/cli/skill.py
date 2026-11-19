"""``ben skill``：check / diagnose / verify。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from Ben.kb.attribution import diagnose_term
from Ben.kb.contract import ExitCode
from Ben.kb.inventory import check_term
from Ben.kb.report import emit
from Ben.kb.verify import verify_term

__all__ = ["run_skill"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ben skill",
        description="Skill KB 校验：check（可达）/ diagnose（归因）/ verify（正样本复现）",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_check = sub.add_parser("check", help="知识取得到吗（零网络零写盘）")
    p_check.add_argument("term", nargs="?", default=None, help="用户叫法；省略则校验全部")
    p_check.add_argument("--all", action="store_true", help="校验全部条目（与 term 互斥）")
    p_check.add_argument(
        "--strict",
        action="store_true",
        help="追加判据结构要求（Validation ≥1 + False Positives）",
    )
    p_check.add_argument("--json", action="store_true", help="机器可读输出")

    p_diag = sub.add_parser("diagnose", help="取不到是叫法问题还是内容问题")
    p_diag.add_argument("term", help="用户叫法（必填）")
    p_diag.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="V",
        help="手动补充候选写法，可重复",
    )
    p_diag.add_argument(
        "--strict-contract",
        action="store_true",
        help="变体 < 2 一律报用法错误（恢复 D3 原语义）",
    )
    p_diag.add_argument("--json", action="store_true", help="机器可读输出")

    p_verify = sub.add_parser("verify", help="正样本上 skill 能否复现")
    p_verify.add_argument("term", help="用户叫法")
    p_verify.add_argument("--profile", required=True, help="靶场 profile 名")
    p_verify.add_argument(
        "--mode",
        choices=("scan", "probe"),
        default="scan",
        help="scan=聚焦扫描；probe=只跑 oracle 预检（零模型）",
    )
    p_verify.add_argument("--max-budget", type=float, default=None, metavar="USD")
    p_verify.add_argument("--max-turns", type=int, default=None, metavar="N")
    p_verify.add_argument("--minutes", type=int, default=None, metavar="N")
    p_verify.add_argument("--out-dir", type=Path, default=None)
    p_verify.add_argument(
        "--oracle-only",
        action="store_true",
        help="同 --mode probe：只做 oracle 预检",
    )
    p_verify.add_argument("--json", action="store_true")

    return parser


def run_skill(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[2:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else int(ExitCode.USAGE)

    if args.verb == "check":
        if args.term and args.all:
            print("<term> 与 --all 互斥", file=sys.stderr)
            return int(ExitCode.USAGE)
        result = check_term(
            args.term,
            strict=bool(args.strict),
            all_terms=bool(args.all) or args.term is None,
        )
        return emit(result, as_json=bool(args.json))

    if args.verb == "diagnose":
        result = diagnose_term(
            args.term,
            extra_variants=list(args.variant or []),
            strict_contract=bool(args.strict_contract),
        )
        return emit(result, as_json=bool(args.json))

    if args.verb == "verify":
        result = verify_term(
            args.term,
            profile_name=args.profile,
            mode=args.mode,
            max_budget=args.max_budget,
            max_turns=args.max_turns,
            minutes=args.minutes,
            out_dir=args.out_dir,
            oracle_only=bool(args.oracle_only) or args.mode == "probe",
        )
        # 人读：CONFIRMED 时打印判据原文
        if not args.json and result.code == ExitCode.OK and result.rows:
            code = emit(result, as_json=False)
            for row in result.rows:
                if isinstance(row, dict) and row.get("result") == "PASS":
                    print(f"  #{row.get('index')} 通过  {row.get('text')!r}")
                    for ref in row.get("evidence") or []:
                        print(f"          ← {ref}")
            return code
        return emit(result, as_json=bool(args.json))

    print(f"未知子命令：{args.verb}", file=sys.stderr)
    return int(ExitCode.USAGE)
