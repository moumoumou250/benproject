"""工具包（对齐 strix 的 tools 目录结构）。

面向模型的 @function_tool 工具按族存放于 ``<family>/tool.py``，
由 :mod:`Ben.agents.factory` 直接深层导入；``mcp/`` 为 MCP 客户端
基础设施及其 agent 工具（agent_tools.py）。shell 工具（exec_command /
write_stdin）不在本包——由 SDK 的 ``Shell`` capability 绑定 live
sandbox session 生成（对齐 strix）。

使用时深层导入，避免 ``import Ben.tools`` 时拉入全部子模块依赖。
"""
