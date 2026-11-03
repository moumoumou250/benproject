"""Ben 包：漏洞测试 Agent 实现（对应 strix 的 ``strix/`` 包）。

- ``agents/``：Agent 构建与预设 Prompt（factory.py / prompt.py / prompts/）；
- ``config/``：应用配置（settings.py）；
- ``core/``：顶层编排（runner.py）；
- ``interface/``：命令行入口（main.py / cli_args.py）；
- ``llm/``：OpenAI 兼容大模型客户端；
- ``runtime/``：Docker 连接与容器执行；
- ``tools/``：工具注册、Schema 校验与工具实现。
"""

import os

# LiteLLM 默认在 import 时从 GitHub 拉取价格表，网络受限时会重试 3 次、拖慢启动
# 约 45 秒。默认改用随包内置的价格表；需要最新价格时可显式设置
# LITELLM_LOCAL_MODEL_COST_MAP=false。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
