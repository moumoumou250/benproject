"""读取 MCP 服务器配置文件（fail-open）。

默认读取 ~/.ben/mcp-servers.json，可用环境变量 BEN_MCP_CONFIG 覆盖。
文件是 JSON 列表，格式与 strix 的 mcp-servers.json 一致。

失败一律不阻断任务：
- 文件不存在 / 不可读 / 不是列表 -> 返回空列表；
- 单个条目非法 -> 记录告警并跳过；
- 同名连接 -> 保留第一个，后面的丢弃并告警。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from Ben.tools.mcp.config import McpConnectionConfig

logger = logging.getLogger(__name__)

# 默认配置路径（与 strix 的 ~/.strix/mcp-servers.json 对应）
_DEFAULT_PATH: Path = Path.home() / ".ben" / "mcp-servers.json"
_PATH_ENV_VAR = "BEN_MCP_CONFIG"


def _resolve_path(path: Path | None) -> Path:
    """配置路径优先级：显式参数 > 环境变量 > 默认路径。"""
    if path is not None:
        return path
    override = os.environ.get(_PATH_ENV_VAR)
    if override:
        return Path(override)
    return _DEFAULT_PATH


def load_mcp_configs(path: Path | None = None) -> list[McpConnectionConfig]:
    """加载 MCP 连接配置列表。

    文件缺失或无法解析时返回空列表；非法条目跳过；同名连接去重（第一个赢）。
    """
    source = _resolve_path(path)
    if not source.exists():
        return []

    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("无法读取 MCP 配置文件：%s，已忽略", source)
        return []

    if not isinstance(raw, list):
        logger.warning("MCP 配置文件 %s 不是 JSON 列表，已忽略", source)
        return []

    configs: list[McpConnectionConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        try:
            config = McpConnectionConfig.from_dict(entry)
        except ValueError as exc:
            logger.warning(
                "跳过 %s 中第 %d 个非法 MCP 配置：%s", source, index, exc
            )
            continue
        if config.name in seen:
            logger.warning(
                "MCP 连接名 %r 重复，保留第一个，后续条目丢弃", config.name
            )
            continue
        seen.add(config.name)
        configs.append(config)
    return configs
