"""BenProject 入口：转发到 Ben.interface.main。

与 strix 的 ``strix/interface/main.py``（由 ``[project.scripts]`` 暴露）对应，
这里保留根目录 ``main.py`` 作为直接运行入口：

    .venv/Scripts/python.exe main.py http://juice-shop:3000/

等价于：

    .venv/Scripts/python.exe -m Ben.interface.main http://juice-shop:3000/
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Ben.interface.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
