"""预设 Prompt 的加载。

Prompt 文本放在 ``Ben/agents/prompts/``（对应 strix 的
``strix/agents/prompts``），由运行器通过 :func:`load_system_prompt` 读取，
不在源码里硬编码。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_system_prompt(
    prompts_dir: Path, filename: str = "vulnerability_testing.txt"
) -> str:
    """读取预设系统 Prompt 文本；文件不存在时抛出 FileNotFoundError。"""
    prompt_path = Path(prompts_dir) / filename
    if not prompt_path.exists():
        raise FileNotFoundError(f"预设 Prompt 文件不存在：{prompt_path}")
    text = prompt_path.read_text(encoding="utf-8")
    logger.info("已加载预设 Prompt：%s（%d 字符）", prompt_path.name, len(text))
    return text
