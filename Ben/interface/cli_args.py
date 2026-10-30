"""命令行参数解析（对应 strix 的 interface/cli_args.py）。"""

from __future__ import annotations

import argparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BenProject 最小漏洞测试 Agent（DeepSeek + Docker）"
    )

    parser.add_argument("target", nargs="?", default="", help="授权目标 URL")
    parser.add_argument("--selftest-docker", action="store_true", help="仅验证 Docker 执行器")
    parser.add_argument("--selftest-llm", action="store_true", help="仅验证 DeepSeek 文本调用")
    parser.add_argument("--selftest-tools", action="store_true", help="仅验证工具层真实执行")
    parser.add_argument("--keep-container", action="store_true", help="任务结束保留执行容器")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser.parse_args()
    """
        args.target = "http://juice-shop:3000/"
        以下参数未出现，默认值为 False
        args.selftest_docker = False
        args.selftest_llm = False
        args.selftest_tools = False
        args.keep_container = False
        args.verbose = False
    """