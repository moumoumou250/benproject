"""统一原子写 JSON / 追加 JSONL（带 fsync）。

claim 丢失会直接造成重复劳动，必须 fsync；其余编排账本顺带统一。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def atomic_write_json(path: Path, obj: Any, *, indent: int | None = 2) -> None:
    """写临时文件 → fsync → replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=indent, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.debug("jsonstore: failed to remove tmp %s", tmp, exc_info=True)
        raise


def append_jsonl(path: Path, obj: Any) -> None:
    """追加一行 JSON；flush + fsync。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
