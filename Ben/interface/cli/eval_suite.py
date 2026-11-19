"""``ben eval`` — 正样本靶场 recall 对照（覆盖完整性 P3 骨架）。

对照一份已知漏洞清单与某次 run 的 ``vulnerabilities.json`` /
``coverage.json``，输出 recall 与未覆盖面。不跑扫描；只读产物。

清单格式（YAML 或 JSON）::

    name: juice-shop-smoke
    expected:
      - id: sqli-login
        type: sql-injection
        match: ["SQL", "injection", "/rest/user/login"]
      - id: xss-search
        type: xss
        match: ["XSS", "search"]
    surfaces:          # 可选：应出现在 coverage 的 surface 子串
      - /rest/user/login
      - /api/Users
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ben eval",
        description="对照已知漏洞清单计算 recall / uncovered（只读 run 产物）",
    )
    parser.add_argument(
        "--suite",
        required=True,
        metavar="PATH",
        help="清单文件（.yaml/.yml/.json）或 docs/eval-suites/<name>.yaml",
    )
    parser.add_argument(
        "--run",
        required=True,
        metavar="DIR",
        help="run 目录（含 vulnerabilities.json / coverage.json）",
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    return parser


def _resolve_suite(raw: str) -> Path:
    path = Path(raw)
    if path.is_file():
        return path
    candidate = Path("docs/eval-suites") / f"{raw}.yaml"
    if candidate.is_file():
        return candidate
    candidate_json = Path("docs/eval-suites") / f"{raw}.json"
    if candidate_json.is_file():
        return candidate_json
    raise FileNotFoundError(f"suite not found: {raw}")


def _load_suite(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "PyYAML required for .yaml suites; use .json or pip install pyyaml"
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("suite root must be a mapping")
    return data


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _finding_blob(reports: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for report in reports:
        parts.append(json.dumps(report, ensure_ascii=False, default=str))
    return "\n".join(parts).lower()


def _match_expected(blob: str, expected: dict[str, Any]) -> bool:
    needles = expected.get("match") or []
    if not isinstance(needles, list) or not needles:
        typ = str(expected.get("type") or "").lower()
        return bool(typ) and typ in blob
    return all(str(n).lower() in blob for n in needles)


def evaluate(*, suite: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    vulns_raw = _load_json(run_dir / "vulnerabilities.json")
    coverage_raw = _load_json(run_dir / "coverage.json")
    reports: list[dict[str, Any]] = []
    if isinstance(vulns_raw, list):
        reports = [r for r in vulns_raw if isinstance(r, dict)]
    elif isinstance(vulns_raw, dict):
        inner = vulns_raw.get("vulnerabilities") or vulns_raw.get("findings") or []
        if isinstance(inner, list):
            reports = [r for r in inner if isinstance(r, dict)]

    blob = _finding_blob(reports)
    expected_list = suite.get("expected") or []
    if not isinstance(expected_list, list):
        expected_list = []

    hits: list[str] = []
    misses: list[str] = []
    for item in expected_list:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("id") or item.get("type") or "?")
        if _match_expected(blob, item):
            hits.append(eid)
        else:
            misses.append(eid)

    total = len(hits) + len(misses)
    recall = (len(hits) / total) if total else 1.0

    uncovered: list[dict[str, Any]] = []
    if isinstance(coverage_raw, dict):
        section = coverage_raw.get("not_covered")
        if isinstance(section, dict) and isinstance(section.get("items"), list):
            uncovered = [i for i in section["items"] if isinstance(i, dict)]
        else:
            for entry in coverage_raw.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("outcome") in {"pending", "needs_follow_up"}:
                    uncovered.append(entry)

    wanted_surfaces = suite.get("surfaces") or []
    surface_misses: list[str] = []
    if isinstance(wanted_surfaces, list) and wanted_surfaces:
        covered_text = json.dumps(coverage_raw or {}, ensure_ascii=False).lower()
        for surface in wanted_surfaces:
            if str(surface).lower() not in covered_text:
                surface_misses.append(str(surface))

    return {
        "suite": suite.get("name") or "unnamed",
        "run_dir": str(run_dir),
        "expected_total": total,
        "hits": hits,
        "misses": misses,
        "recall": round(recall, 4),
        "findings_filed": len(reports),
        "uncovered_surfaces": len(uncovered),
        "surface_misses": surface_misses,
        "not_covered_sample": uncovered[:20],
    }


def run_eval(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        suite_path = _resolve_suite(args.suite)
        suite = _load_suite(suite_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[FAIL] suite: {exc}", file=sys.stderr)
        return 2
    run_dir = Path(args.run)
    if not run_dir.is_dir():
        print(f"[FAIL] run dir not found: {run_dir}", file=sys.stderr)
        return 2
    result = evaluate(suite=suite, run_dir=run_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"suite: {result['suite']}")
        print(f"run:   {result['run_dir']}")
        print(
            f"recall: {result['recall']:.1%}  "
            f"({len(result['hits'])}/{result['expected_total']})"
        )
        if result["misses"]:
            print("misses: " + ", ".join(result["misses"]))
        print(f"findings_filed: {result['findings_filed']}")
        print(f"uncovered_surfaces: {result['uncovered_surfaces']}")
        if result["surface_misses"]:
            print("surface_misses: " + ", ".join(result["surface_misses"]))
    # recall < 1 → exit 3，便于 CI；清单空则 0
    if result["expected_total"] and result["misses"]:
        return 3
    return 0
