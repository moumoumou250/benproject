"""Ben.core：顶层编排层。

- ``runner.py``：openai-agents SDK 驱动的 Agent 运行器（对应 strix 的
  ``strix/core/runner.py`` + ``strix/core/execution.py``）。使用 SDK 的
  ``Runner.run_streamed()`` + ``RunConfig`` 执行 agent 循环，
  工具由 @function_tool 装饰函数实现，模型决策与工具执行仍严格分离。
"""
