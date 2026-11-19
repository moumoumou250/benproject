"""``ben transcript <run>``：读 audit/events.jsonl 回放。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from Ben.core.audit import load_events, redact, render_markdown
from Ben.core.paths import latest_run_dir, resolve_run_dir, runs_base_dir


def _resolve(arg: str | None) -> Path:
    if arg:
        candidate = Path(arg).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        found = resolve_run_dir(arg)
        if found is None:
            raise SystemExit(f"找不到 run：{arg}")
        return found
    latest = latest_run_dir()
    if latest is None:
        raise SystemExit(f"{runs_base_dir()} 下还没有任何 run")
    return latest


def run_transcript(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ben transcript", description="回放 run 的 audit transcript")
    parser.add_argument("run", nargs="?", help="run 名或目录（默认最近一次）")
    parser.add_argument("--agent", help="只看某个 agent id")
    parser.add_argument("--turn", type=int, help="只看某一轮 llm_start/end")
    parser.add_argument("--md", action="store_true", help="输出 markdown（默认）")
    parser.add_argument("--json", action="store_true", help="输出 events JSON")
    parser.add_argument("--redact", action="store_true", help="脱敏后输出")
    args = parser.parse_args(argv)

    run_dir = _resolve(args.run)
    audit_dir = run_dir / "audit"
    if not audit_dir.is_dir():
        print(f"无 audit 目录：{audit_dir}（需 BEN_AUDIT=1 跑过扫描）", file=sys.stderr)
        return 1

    events = load_events(audit_dir)
    if args.json:
        text = json.dumps(events, ensure_ascii=False, indent=2)
    else:
        text = render_markdown(events, agent=args.agent, turn=args.turn)

    if args.redact:
        text = redact(text)
        print("(已脱敏；含疑似凭据时请勿明文外传)", file=sys.stderr)

    index = audit_dir / "index.json"
    if index.is_file() and not args.json:
        try:
            meta = json.loads(index.read_text(encoding="utf-8"))
            hits = int(meta.get("secret_hits") or 0)
            if hits and not args.redact:
                print(f"(index 记 secret_hits={hits}；对外分享请加 --redact)", file=sys.stderr)
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0
