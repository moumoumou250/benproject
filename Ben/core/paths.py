"""Run directory path helpers.

产物的物理位置统一收在 ``_artifacts/`` 下：

- 新的扫描产物写入 ``_artifacts/runs/<run>``；
- 报告与轨迹写入 ``_artifacts/reports/``（见 :mod:`Ben.config.settings`）。

历史版本把产物直接放在仓库根的 ``strix_runs/``。那批目录已经存在且
不参与版本控制，因此读取侧继续兼容该位置（:func:`runs_base_dirs`），
但不再写入 —— 写入一律走新位置，避免根目录继续堆产物。
"""

from __future__ import annotations

from pathlib import Path

# 所有运行产物的统一根目录。
ARTIFACTS_DIR_NAME = "_artifacts"

# 新产物的写入位置（相对于项目根）。
RUNS_DIR_NAME = f"{ARTIFACTS_DIR_NAME}/runs"

# 历史产物位置，只读兼容，不再写入。
LEGACY_RUNS_DIR_NAME = "strix_runs"

RUNTIME_STATE_DIR_NAME = ".state"
RUN_RECORD_FILENAME = "run.json"


def run_dir_for(run_name: str, *, cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return base / RUNS_DIR_NAME / run_name


def runtime_state_dir(run_dir: Path) -> Path:
    return run_dir / RUNTIME_STATE_DIR_NAME


def run_record_path(run_dir: Path) -> Path:
    return run_dir / RUN_RECORD_FILENAME


def runs_base_dir(*, cwd: Path | None = None) -> Path:
    """新产物的 runs 根目录（写入用，可能尚不存在）。"""
    base = cwd or Path.cwd()
    return base / RUNS_DIR_NAME


def legacy_runs_base_dir(*, cwd: Path | None = None) -> Path:
    """历史产物目录（只读兼容）。"""
    base = cwd or Path.cwd()
    return base / LEGACY_RUNS_DIR_NAME


def runs_base_dirs(*, cwd: Path | None = None) -> list[Path]:
    """读取用的全部 runs 根目录，新位置在前，仅返回已存在的。"""
    base = cwd or Path.cwd()
    candidates = [runs_base_dir(cwd=base), legacy_runs_base_dir(cwd=base)]
    return [path for path in candidates if path.is_dir()]


def latest_run_dir(*, cwd: Path | None = None) -> Path | None:
    base = cwd or Path.cwd()
    candidates = [
        child
        for runs_dir in runs_base_dirs(cwd=base)
        for child in runs_dir.iterdir()
        if run_record_path(child).is_file()
    ]
    if not candidates:
        return None
    # run.json is rewritten on status/end changes, so its mtime tracks activity
    # more reliably than the directory mtime (a live run sorts to the top).
    return max(candidates, key=lambda child: run_record_path(child).stat().st_mtime)


def all_run_dirs(*, cwd: Path | None = None) -> list[Path]:
    """全部 run 目录（含历史位置），按目录名去重，新位置优先。"""
    base = cwd or Path.cwd()
    seen: dict[str, Path] = {}
    for runs_dir in runs_base_dirs(cwd=base):
        for child in runs_dir.iterdir():
            if child.is_dir():
                seen.setdefault(child.name, child)
    return sorted(seen.values(), key=lambda path: path.name)


def existing_run_dir(run_name: str, *, cwd: Path | None = None) -> Path | None:
    """按名字找已存在的 run 目录（新位置优先，其次历史位置）。"""
    for runs_dir in runs_base_dirs(cwd=cwd):
        candidate = runs_dir / run_name
        if candidate.is_dir():
            return candidate
    return None


def resolve_run_dir(run_name: str, *, cwd: Path | None = None) -> Path:
    """读取既有 run 时使用：优先命中已存在的目录，否则返回新位置路径。

    resume / 查看历史 run 必须跨 ``_artifacts/runs`` 与 ``strix_runs`` 解析，
    否则迁移过渡期的历史扫描会变成"找不到"。
    """
    return existing_run_dir(run_name, cwd=cwd) or run_dir_for(run_name, cwd=cwd)
