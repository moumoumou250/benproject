"""人读 / JSON 统一出口。"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from Ben.kb.contract import Result


def emit(result: Result, *, as_json: bool = False, out: TextIO | None = None) -> int:
    """结果行打 stdout；用法/环境错误打 stderr。返回退出码。"""
    stdout = out or sys.stdout
    if as_json:
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2), file=stdout)
        return int(result.code)

    rows = result.rows or []
    if rows and isinstance(rows[0], dict) and "reachable" in rows[0]:
        # check 表格式
        width = max((len(str(r.get("term", ""))) for r in rows), default=8)
        for row in rows:
            term = str(row.get("term", ""))
            ok = bool(row.get("reachable"))
            status = "可达" if ok else "不可达"
            detail = str(row.get("detail") or "")
            print(f"{term:<{width}}  {status}  {detail}".rstrip(), file=stdout)
        return int(result.code)

    # diagnose / 通用
    attribution = result.attribution
    code = int(result.code)
    label = result.message or (str(attribution) if attribution else "")
    term = ""
    if rows and isinstance(rows[0], dict):
        term = str(rows[0].get("term") or "")
    head = f"{term}  →  {code} {label}".strip() if term else f"{code} {label}".strip()
    print(head, file=stdout)

    hint = (result.extra or {}).get("hint")
    if hint:
        print(f"  {hint}", file=stdout)
    file_path = (result.extra or {}).get("file")
    if file_path:
        print(f"  文件：{file_path}", file=stdout)
    if result.action:
        cls = result.action.get("class")
        ahint = result.action.get("hint")
        print(f"  动作：{cls} {ahint}", file=stdout)
    elif (result.extra or {}).get("variants") == 1 and code == 0 and not attribution:
        print("  提示：该条目没有别名，反查能力有限", file=stdout)

    return int(result.code)
