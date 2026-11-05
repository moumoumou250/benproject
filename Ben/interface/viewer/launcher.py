"""Local single-flight scan launcher; subprocess arguments never pass through a shell."""

from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
from urllib.parse import urlsplit


class ScanLauncher:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.previous: set[str] = set()
        self.target = ""
        self.control_path: Path | None = None
        self.log_path = self.root / "reports" / "web-scan.log"

    def status(self) -> dict:
        with self.lock:
            return self._status()

    def _status(self) -> dict:
        code = self.process.poll() if self.process else None
        candidates = (
            [
                p
                for p in (self.root / "strix_runs").glob("*")
                if p.name not in self.previous and (p / "run.json").is_file()
            ]
            if self.process
            else []
        )
        run = min(candidates, key=lambda p: p.stat().st_mtime).name if candidates else None
        return {
            "running": self.process is not None and code is None,
            "exit_code": code,
            "paused": bool(self.process and code is None and self.control_path and self.control_path.exists()),
            "run": run,
            "target": self.target,
            "log": self.log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
            if self.process and self.log_path.exists()
            else "",
        }

    def set_paused(self, paused: bool) -> dict:
        with self.lock:
            if not self.process or self.process.poll() is not None or not self.control_path:
                raise RuntimeError("没有正在运行的测试")
            if paused:
                self.control_path.touch()
            else:
                self.control_path.unlink(missing_ok=True)
            return self._status()

    def start(self, body: dict) -> dict:
        target = body.get("target", "")
        if not isinstance(target, str) or len(target) > 2048:
            raise ValueError("请输入有效的网站 URL")
        target = target.strip()
        if target and "://" not in target and not target.startswith(("-", "/")):
            target = "http://" + target
        parsed = urlsplit(target)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("URL 端口无效") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(c.isspace() for c in target)
        ):
            raise ValueError("目标必须是 http/https 网站地址，不能包含凭据")
        mode = body.get("mode", "quick")
        if mode not in {"quick", "standard", "deep"}:
            raise ValueError("扫描模式无效")
        instruction = body.get("instruction", "")
        if not isinstance(instruction, str) or len(instruction) > 12000:
            raise ValueError("测试要求最多 12000 字符")
        try:
            turns = int(body.get("max_turns", 20))
            budget = float(body.get("budget", 1))
        except (ValueError, TypeError) as exc:
            raise ValueError("轮数或预算无效") from exc
        if not 1 <= turns <= 200 or not math.isfinite(budget) or not 0 < budget <= 100:
            raise ValueError("轮数应为 1–200，预算应大于 0 且不超过 100 美元")
        with self.lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("已有测试正在运行，请等待完成")
            self.previous = {p.name for p in (self.root / "strix_runs").glob("*")}
            self.target = target.strip()
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # Independent server sessions must not contend for one Windows log handle.
            self.log_path = self.log_path.parent / f"web-scan-{uuid.uuid4().hex[:12]}.log"
            command = [
                sys.executable,
                "-m",
                "Ben.interface.main",
                "-n",
                "-t",
                self.target,
                "-m",
                mode,
                "--max-turns",
                str(turns),
                "--max-budget",
                str(budget),
            ]
            if instruction.strip():
                command.extend(["--instruction", instruction.strip()])
            self.control_path = self.log_path.with_suffix(".pause")
            environment = dict(os.environ, BEN_SCAN_PAUSE_FILE=str(self.control_path))
            try:
                with self.log_path.open("w", encoding="utf-8") as output:
                    self.process = subprocess.Popen(
                        command,
                        cwd=self.root,
                        env=environment,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
            except OSError as exc:
                raise RuntimeError(f"无法启动扫描，请检查运行环境及报告目录写入权限：{exc}") from exc
            return self._status()
