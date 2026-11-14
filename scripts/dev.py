"""Cross-platform development commands: python scripts/dev.py --help."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TUI = ROOT / "Ben/interface/tui"
FRONTEND = ROOT / "Ben/interface/viewer/frontend"


def run(command: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    executable = shutil.which(command[0])
    if executable is None:
        raise SystemExit(f"Required executable not found: {command[0]}")
    subprocess.run([executable, *command[1:]], cwd=cwd, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "task",
        choices=[
            "test",
            "lint",
            "format-check",
            "smoke",
            "tui-build",
            "tui-test",
            "tui-lint",
            "viewer",
            "package",
        ],
    )
    task = parser.parse_args().task
    if task in {"test", "lint", "format-check", "smoke"}:
        commands = {
            "test": [sys.executable, "-m", "pytest", "-q"],
            "lint": [sys.executable, "-m", "ruff", "check", "."],
            "format-check": [sys.executable, "-m", "ruff", "format", "--check", "."],
            "smoke": [sys.executable, "scripts/smoke_core.py"],
        }
        run(commands[task])
    elif task == "tui-build":
        output = ROOT / "Ben/bin" / ("ben-tui.exe" if os.name == "nt" else "ben-tui")
        output.parent.mkdir(parents=True, exist_ok=True)
        run(
            ["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(output), "./cmd/ben-tui"],
            TUI,
            {**os.environ, "CGO_ENABLED": "0"},
        )
    elif task == "tui-test":
        run(["go", "test", "-race", "./..."], TUI)
    elif task == "tui-lint":
        run(["go", "vet", "./..."], TUI)
        result = subprocess.run(
            [shutil.which("gofmt") or "gofmt", "-l", "."],
            cwd=TUI,
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip():
            raise SystemExit("Run gofmt on:\n" + result.stdout)
    elif task == "viewer":
        run(["npm", "ci"], FRONTEND)
        run(["npm", "run", "typecheck"], FRONTEND)
        run(["npm", "run", "build"], FRONTEND)
    else:
        run([sys.executable, "-m", "build", "--wheel"])


if __name__ == "__main__":
    main()
