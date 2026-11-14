"""Hatchling 构建钩子：编译并打包 Go TUI sidecar（移植自 strix 的同名钩子）。

与 strix 的差异：
- 包名/二进制名 strix → ben（输出 ``Ben/bin/ben-tui``）；
- 平台标签开关 ``STRIX_WHEEL_PLATFORM_TAG`` → ``BEN_WHEEL_PLATFORM_TAG``；
- 构建目录 ``build/sidecar``，源码目录 ``Ben/interface/tui``。

注意：Ben 日常以源码方式运行（``python main.py`` / ``python -m Ben.interface.main``），
本钩子只在 ``python -m build`` / ``pip wheel`` 时生效。缺少 Go 工具链时打包会失败，
这是刻意的：交互式 TUI 只能由该二进制提供。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sysconfig
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    """编译 Bubble Tea sidecar 并随 wheel 分发。"""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        # editable 安装直接跑仓库里的源码（go run），没有可打包的产物。
        if version == "editable":
            return

        root = Path(self.root)
        executable = "ben-tui.exe" if os.name == "nt" else "ben-tui"
        output = root / "build" / "sidecar" / executable
        output.parent.mkdir(parents=True, exist_ok=True)

        go = shutil.which("go")
        if go is None:
            raise RuntimeError("构建 Bubble Tea TUI 需要 Go 1.24 或更新版本")
        env = os.environ.copy()
        env["CGO_ENABLED"] = "0"
        subprocess.run(  # noqa: S603 - 固定构建命令，使用解析到的 go 可执行文件
            [
                go,
                "build",
                "-trimpath",
                "-ldflags=-s -w",
                "-o",
                str(output),
                "./cmd/ben-tui",
            ],
            cwd=root / "Ben" / "interface" / "tui",
            env=env,
            check=True,
        )

        build_data["force_include"][str(output)] = f"Ben/bin/{executable}"
        build_data["pure_python"] = False
        platform_tag = os.environ.get("BEN_WHEEL_PLATFORM_TAG")
        if not platform_tag:
            platform_tag = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        build_data["tag"] = f"py3-none-{platform_tag}"
