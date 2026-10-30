"""Ben 包：漏洞测试 Agent 实现（对应 strix 的 ``strix/`` 包）。

- ``agents/``：Agent 构建与预设 Prompt（factory.py / prompt.py / prompts/）；
- ``config/``：应用配置（settings.py）；
- ``core/``：顶层编排（runner.py）；
- ``interface/``：命令行入口（main.py / cli_args.py）；
- ``llm/``：OpenAI 兼容大模型客户端；
- ``runtime/``：Docker 连接与容器执行；
- ``tools/``：工具注册、Schema 校验与工具实现。
"""
