"""把 run 目录下的沙箱回传证据合并进 verify 的 http_dir（唯一入口）。"""

from __future__ import annotations

from pathlib import Path

from Ben.kb.evidence import Evidence, EvidenceKind, collect_from_http_dir


def merge_workspace_evidence(run_dir: Path, http_dir: Path) -> list[Evidence]:
    """递归拷贝 ``run_dir/.evidence/**`` → ``http_dir/**``，再收集为 e2。"""
    workspace_ev = Path(run_dir) / ".evidence"
    if not workspace_ev.is_dir():
        return collect_from_http_dir(http_dir, kind=EvidenceKind.E2)

    http_dir.mkdir(parents=True, exist_ok=True)
    for src in workspace_ev.rglob("*"):
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(workspace_ev)
        except ValueError:
            continue
        if ".." in rel.parts:
            continue
        dest = http_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
    return collect_from_http_dir(http_dir, kind=EvidenceKind.E2)
