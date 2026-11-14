"""Ben.agents：Agent 构建与模板化系统提示词。

- ``factory.py``：基于 openai-agents SDK 的 SandboxAgent 构建（实验路径）；
- ``prompt.py``：Jinja2 模板化提示词渲染器（机制等价 strix/agents/prompt.py），
  对外 :func:`load_system_prompt` 仍返回一个字符串；
- ``prompts/``：主模板 ``system_prompt.jinja`` 与 ``scope.jinja``（原
  vulnerability_testing.txt 的七节内容已逐字迁入主模板）。
"""
