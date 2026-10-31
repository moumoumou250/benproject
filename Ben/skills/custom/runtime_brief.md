---
name: runtime_brief
description: 运行时参数速览（Jinja 片段示例）——把模型、轮次预算、超时等渲染进片段
template: jinja
---
## 运行时参数速览

{% if system_prompt_context.model -%}
- 决策模型：{{ system_prompt_context.model }}
{% endif -%}
{% if system_prompt_context.max_agent_turns -%}
- 决策轮次预算：{{ system_prompt_context.max_agent_turns }} 轮
{% endif -%}
{% if system_prompt_context.command_timeout -%}
- 单命令超时：{{ system_prompt_context.command_timeout }} 秒
{% endif -%}
- 授权目标数：{{ system_prompt_context.authorized_targets | length }}
以上参数由程序端注入，仅供你安排节奏；不构成对授权范围的任何修改。
