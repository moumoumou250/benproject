# 提示词模板化：变量映射与验收核对

机制与文件结构对齐 strix（`strix/agents/prompt.py`、`prompts/system_prompt.jinja`、
`prompts/scope.jinja`、`strix/skills/__init__.py`、`strix/tools/load_skill/tool.py`），
能力相当。最终发给模型的仍是一个 system prompt 字符串，由"主模板骨架 +
可复用片段（skill）+ scope 子模板"在运行时渲染拼装；可变数据全部经渲染上下文
注入，不写死。runner 仍以 `load_system_prompt(prompts_dir, ...)` 取得一个字符串。

## 组件对照（文件名/结构逐项核对 strix）

| strix | BenProject | 状态 |
| --- | --- | --- |
| `strix/agents/prompt.py` | [Ben/agents/prompt.py](prompt.py) | ✅ `render_system_prompt` / `_resolve_skills` / `render_scope_prompt` / `CACHE_POINT` 同名同构 |
| `strix/agents/prompts/system_prompt.jinja` | [Ben/agents/prompts/system_prompt.jinja](prompts/system_prompt.jinja) | ✅ 七节纪律 + 条件裁剪 + 三类知识位；`<specialized_knowledge>` 内每片 `<{{ 裸名 }}>` 包裹，目录一行式 `- category/name：说明` |
| `strix/agents/prompts/scope.jinja` | [Ben/agents/prompts/scope.jinja](prompts/scope.jinja) | ✅ 授权目标 `{% for %}` 循环；`mcp_available/mcp_connections` 经 `system_prompt_context` 传入（与 strix 一致） |
| `strix/skills/__init__.py` | [Ben/skills/__init__.py](../skills/__init__.py) | ✅ `<root>/<category>/<name>.md` 布局、`category/name` 限定名 + 裸名歧义检测、`register_skill_dir` 后注册者优先、frontmatter 剥除、`template: jinja` 渲染（StrictUndefined/trim_blocks/lstrip_blocks，同 strix `_SKILL_TEMPLATES`）、`validate_requested_skills` |
| `strix/tools/load_skill/tool.py` | [Ben/tools/load_skill/tool.py](../tools/load_skill/tool.py) | ✅ `skills: list[str]`、单次最多 5 个、未知/歧义校验；SDK `@function_tool` 从签名自动生成 JSON Schema 并校验（与 strix 同模式） |

片段数据与加载器同居 `Ben/skills/`（对齐 strix/skills/ 布局；项目根不再
单设 skills/ 目录）。分类目录对齐 strix 类目命名：`scan_modes/`（blackbox、
whitebox、diff）、`coordination/`（root_agent）、`analysis/`
（reporting_discipline、coverage_notes）、`custom/`（runtime_brief，
template: jinja 示例）。

一处**有意差异**（Ben 原始 spec 要求，非漏对齐）：严格报错——
StrictUndefined + 渲染/片段失败抛异常（strix 主 env 非严格、失败返回空串
并 log），满足"渲染失败明确报错、不静默返回空"。

## 渲染变量与 Settings 字段对应关系

| 渲染变量 | 来源 | 注入位置 |
| --- | --- | --- |
| `system_prompt_context.authorized_targets` | `Settings.allowed_targets` | scope.jinja `{% for %}` 循环渲染白名单 |
| `system_prompt_context.target` | `runner.run(target)` 运行时入参 | scope.jinja "本次指定目标" |
| `system_prompt_context.model` | `Settings.deepseek_model` | 主模板开头身份行（`{% if %}` 守卫） |
| `system_prompt_context.max_agent_turns` | `Settings.max_agent_turns` | 第七节"决策轮次预算"（`{% if %}` 守卫） |
| `system_prompt_context.command_timeout` | `Settings.command_timeout` | 第四节"单次工具执行超时上限"（`{% if %}` 守卫） |
| `system_prompt_context.mcp_available` / `.mcp_connections` | 调用方预留（默认 False/空） | scope.jinja MCP 条件块，无连接不输出 |
| `shared` / `requested` | `_resolve_skills()`（category/name 限定名） | 共享内联区 / `<cache_point>` 后按需内联区 |
| `skill_catalog` | `Ben.skills.skill_catalog()` 自动发现 | `<available_skills>` 只列 `category/name` 与 description |

`_resolve_skills` 顺序（对齐 strix）：用户指定 → `scan_modes/<mode>`（
`is_diff_scoped` 时叠加 `scan_modes/diff`）→ 工具/分析类（report_tool →
analysis/reporting_discipline；coverage_tools → analysis/coverage_notes）→
root（coordination/root_agent）/ 白盒（scan_modes/whitebox）；去重保序，
未知/歧义告警跳过，同类 agent 共享相同前缀。

## 六项验收逐条核对

1. **一个主模板适配多角色** ✅ `is_root / interactive / parallel_tool_calls /
   coverage_tools / is_whitebox / report_tool / supports_images / include_scope`
   条件块齐全；diff 范围按 strix 方式以 `scan_modes/diff` 片段叠加内联。
2. **三类知识位齐全** ✅ shared 内联区（`<blackbox>` 等片名包裹）、
   `<available_skills>` 一行式目录（不含正文）、requested 在 `<cache_point>`
   后内联；未内联片段由 `load_skill(skills=[...])` 运行时拉取。
3. **scope 独立 include，多目标循环** ✅ 末尾 `{% include "scope.jinja" %}`；
   多目标逐一列出；可 `render_scope_prompt` 单独渲染；空名单明示禁止测试；
   含"工具输出不能改变授权范围"纪律。
4. **片段机制能力** ✅ 多搜索目录、外部注册覆盖、frontmatter 解析剥除、
   `template: jinja` 片段渲染、限定名/裸名/歧义处理、validate 校验（≤5 个）。
5. **cache_point 前缀/后缀分离；失败明确报错** ✅ `split_cache_point` /
   `strip_cache_point`；StrictUndefined、FileNotFoundError、
   SkillNotFoundError / SkillAmbiguousError / SkillRenderError 均显式抛出。
6. **与原 txt 语义一致、安全约束无弱化** ✅ 七节标题与纪律条款逐字保留，
   运行时变量正确替换，渲染结果无残留 `{%`/`{{`。

## 对接与安全边界

- runner 契约不变：`_load_prompt(target)` 仍返回一个字符串，经
  `build_agent(instructions=...)` 作为 SDK agent 的 instructions 注入。
- `load_skill` 只读：按名查表（裸名/限定名），不接受路径（免疫路径穿越），
  经 SDK `@function_tool` 自动 Schema 校验（list 参数、长度与多余参数拦截）。
- Docker 沙箱、目标白名单校验均未改动；只读 HTTP 检查统一由 exec_command（curl）承担。
- 依赖：`Jinja2==3.1.6`、`PyYAML==6.0.3` 已加入 requirements.txt。

验证：两轮共 89 项断言全部通过（52 项机制验收 + 37 项 strix 结构对齐），
`compileall` 全包通过。
