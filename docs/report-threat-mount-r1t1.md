# 报告 / 威胁建模挂载裁剪（R1 + T1）

> 落地日期：2026-11-17。前置：docstring 瘦身（schema-slimming）。

## 机制

[`ProfileSpec.tool_drop`](../Ben/agents/profiles.py)：在 `tool_names is None` 的全量 child 面上减挂，避免重抄整份白名单。

| Profile | 减挂 | 仍挂（相关） |
|---|---|---|
| `exploit` | `create_dependency_report`, `delete_vulnerability_report`, `save_threat_model` | create_vuln / update / list / get；get+amend 威胁 |
| `validate` | `create_vulnerability_report`, `create_dependency_report`, `save_threat_model` | update / delete / list / get；get+amend |
| `sca`（新） | `create_vulnerability_report`, `delete_vulnerability_report`, `save_threat_model` | create_dep / update / list / get；get+amend |
| `default` / `None` | `save_threat_model` | 全量报告写+读；get+amend |
| root | `_ROOT_REPORT_WRITE_DROP` + `amend_threat_model` | list/get 报告；get+save 威胁 |

`report_tool`（是否内联 `reporting_discipline`）改为：实际挂了任一报告写工具才为真。

## 用法

- 动态漏洞猎手：`profile=exploit`
- 验证/降噪：`profile=validate`
- 已知 CVE 依赖：`profile=sca`（`create_agent(..., profile="sca")`）
- 兼容旧 spawn：不传 profile → default，报告面仍全，但无 `save_threat_model`

## 验收

```bash
.venv/bin/python -m pytest tests/agents/test_profiles.py tests/agents/test_agent_tool_registration.py -q
```
