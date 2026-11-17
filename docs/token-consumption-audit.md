# BenProject Token 消耗审计

> 审计日期：2026-11-17 · 方法：渲染真实提示词 + 解析 `strix_runs/*/.state/agents.db` 历史会话 + 读取工具 schema，全部用 `Ben.llm.context_budget.count_tokens` 计数（模型 `grok/grok-4.6`）。
> 结论：**成本主项不是提示词写得多漂亮，而是"每轮重发的固定前缀"（工具 schema + system prompt）和"无上限增长的工具输出历史"。**

---

## 〇、计费口径（先立规矩，否则会优化错方向）

本文所有数字是 **token 计数**，不等于 **账单**。两者差一个缓存系数：

- 缓存层级是 `tools → system → messages`。工具 schema 在最前段，system prompt 紧随其后，二者构成**稳定前缀**，命中 prefix cache 时按 ~0.1x 计费（本项目既有标定，见 `docs/reporting-vs-threat-schema-token-optimization-v1.md`）。
- 因此「截 schema 省 1,940 tok/请求」换算到账单只有 ~200 等效/请求。
- **真正按全价计费的是 `<cache_point>` 之后的增长部分**：工具输出、工具调用参数、邮箱消息。这部分每轮以全价重发，且越到后期越大。
- **更大的杠杆是轮次**：每多一轮 = 多付一整份当前上下文（20k 起步）。少 1 轮 ≈ 省 1 份 20K。

所以后文的优化建议按「账单」而非「计数」排序；把 schema 瘦身放低，把**轮次 / 历史 / 工具输出**放高。
若部署在无前缀缓存的路由（裸模型名直连 `LLM_API_BASE`、部分自建网关），第一部分的计数即等于账单，此时 schema 裁剪的收益会按 10 倍放大。

---

## 一、计量基线：每次 LLM 调用的固定载荷

每一轮请求都要把「工具定义 + system prompt」完整重发一次，这部分与对话内容无关。（可缓存，按 0.1x 计费；无缓存路由按全价。）

| 角色 | 工具数 | 工具 schema | 系统提示词 | 合计 / 次调用 |
|---|---:|---:|---:|---:|
| root（deep 模式） | 28 | 9,155 | 10,644 | **≈ 19.8k tok** |
| child（default / 无 profile） | 44 | 18,668 | 8,501 | **≈ 27.2k tok** |
| child（exploit） | 42 | 16,498 | 8,501 | ≈ 25.0k |
| child（sca） | 42 | 16,345 | 8,501 | ≈ 24.8k |
| child（validate） | 42 | 14,883 | 8,501 | ≈ 23.4k |
| child（enum） | 11 | 1,975 | 8,501 | ≈ 10.5k |
| child（recon） | 8 | 1,266 | 8,501 | ≈ 9.8k |

关键观察：

- **工具 schema 比 system prompt 更大。** child 默认面 18.7k tok，是提示词（8.5k）的 2.2 倍；45 个工具 schema 合计 19,239 tok。
- **`recon`/`enum` 的轻量工具面是唯一做对的地方**（1.3k/2.0k），其余 profile 全部 16k+。
- **报告类工具占 child 固定成本的 23%**：`create_vulnerability_report` 1,978 + `create_dependency_report` 1,823 + `update_vulnerability_report` 1,539 + `delete_...` + `list_reports` + `get_report` = **6,308 tok**，跟着每一次调用走。

### 系统提示词内部构成（root / deep，10,644 tok）

| 段落 | token |
|---|---:|
| 头部（角色 / 反误拒 / 自主模式） | 1,405 |
| 一~五：目标、范围、流程、工具规则、证据分级 | 2,593 |
| **六：报告格式规范** | **2,234** |
| 七：结束方式 + 内联 `coordination/root_agent`（2.4k）+ 技能目录 + `scope.jinja`（1,085） | 4,446 |

报告格式规范单段 2.2k token（占 21%），且以 `is_root` 为分支渲染给 root——而 root 的职责是委派，真正写报告的是子 agent。

---

## 二、真实运行数据（33 个会话复算）

| 指标 | 值 |
|---|---|
| 会话数 / 消息条目 | 33 / 1,325 |
| 历史总 token | 631,796 |
| 单会话均值 | 19,145 tok（条目 40.2） |
| 最大单会话 | 47,483 tok |
| 工具输出中位数 | 141 tok |
| 工具输出均值 | 805 tok |
| 工具输出 P90 | 1,947 tok |
| **单条工具输出最大** | **11,012 tok** |

- 在某次运行的历史里，**工具输出占 61~73%**（38.6k / 63.7k），工具调用参数占 25%，reasoning summary 仅占 5.6%。
- 最大的一条工具输出（31KB / 9.5k tok）**没有任何截断标记**，原样进入历史并伴随后续所有轮次——相当于每轮多付半个 system prompt。

长尾结构非常明显：中位数 141 tok，但 P90 是 1,947，最大值 11,012。**少数几条巨型输出决定了整个会话的长度**——而长度决定了后续每一轮的输入规模。

### 账单模型

前缀缓存生效时，一轮请求的计费近似为：

```
billed ≈ 0.1 × 当前上下文总量 + 1.0 × 本轮新增
```

历史每增长 1k，后续**每一轮**都多 0.1k；轮次越多，累积越接近 `0.1 × Σ上下文`，也就是对轮次成平方。所以：

- **轮次是乘数**：少 1 轮 = 少读一整份当前上下文 + 少一次全价新增。
- **历史长度是基数**：工具输出直接决定基数。
- 固定前缀（20~27k）是每一轮的常数项，按 0.1x 计，砍它有收益但排在后面。

---

## 三、消耗来源清单（按影响排序）

### P0-1 工具 schema 全集常驻，且几乎不做角色裁剪

- `_BASE_TOOLS` 45 个工具，`exploit` / `validate` / `sca` / `default` 全部保留 42~44 个（只 drop 2~3 个）。
- 报告写工具（6.3k tok）在不需要写报告的执行型 child 上也挂着。
- `create_agent` 单只 1,108 tok（参数文档很长），root 每轮都在付。

**量化**：一个 default child 12 轮 = 18,668 × 12 ≈ **224k tok 仅用于重复发送 schema**。

### P0-2 多 agent 各自重付固定前缀，且前缀不共享

每个 child 的 system prompt 因 profile / skills / is_root 不同而不同，**缓存前缀互不命中**：

- root：19.8k × N 轮
- 每个 child：23~27k × N 轮（各自独立预热）

一次 1 root + 6 children、各 8~12 轮的扫描，固定开销约 **1.5M tok**，占输入总量 75% 以上（有 prefix cache 的厂商按 10% 计费，无 cache 的路由按全价）。

### P0-3 工具输出用「字节 + 头尾」截断，定价口径失真

`ContextSettings` 有两套互不一致的上限：

- `tool_output_max_bytes = 50KB` / `tool_output_max_lines = 2000`（`output_store.bound_and_store`，对所有工具）
- `tool_output_max_tokens = 8000`（只对 shell 的 `_apply_shell_output_cap` 生效）

两个问题：

1. **CJK 低估 20~40%**：SDK 侧 `approx_token_count` 用 `bytes / 4`。UTF-8 中文 3 字节/字，31KB 中文被算成 7.8k "token"，实际 9.5k——这就是为什么 9.5k 的输出能穿过 8,000 的上限。
2. **头尾截断保留的是噪音**：`_head_tail` 砍掉中间，agent 拿不到关键段落，只能重跑或 `grep` 重读，**同一份内容进两次历史**，比一次给全更贵。

### P0-4 压缩阈值按「窗口」而非「成本」触发，太晚

`maybe_compact` 的触发线：`window - max(compact_buffer=20k, output_limit)`。

- 200k 窗口的 grok → **180k 才压缩**；131k 的 deepseek → 111k。
- 也就是说历史可以长到 180k tok，此后每一轮都在付 180k 的输入。

成本最优的做法应远早于此（40~60k）。`keep_tokens=8000` 保留尾部是对的，问题在触发线。

### P1-1 skills 内联进 system prompt，属于永久成本

- 84 个技能片段共 166k tok，平均 1,977，最大 4,958。
- `create_agent(skills=[...])` 会把片段**整篇内联**到 child 的 system prompt（`<cache_point>` 之后），此后每轮都带。
- 3 个技能 ≈ 5.9k tok 常驻；5 个 ≈ 10k。对 8 轮的 child 就是 47k~80k。
- `load_skill` 单次上限 24,000 字符（≈12k tok）一次性进历史；好在它有 `already_loaded` 去重。

### P1-2 结论按「值」重复传递（同一份内容进历史 3~4 次）

一条发现的完整文本会经历：

1. child 的 `agent_finish(result_summary=...)` 工具参数（观测 1.7k tok）
2. 邮箱消息推给 parent（观测 2.4k tok 的 user message）
3. parent 的 `finish_scan(executive_summary=...)`（观测 2.8k tok）
4. 落盘 `vulnerabilities/<id>.md` + 报告

每一次都是**全量正文**，而不是引用 id。多 agent 场景下这条链路是乘法。

### P1-3 `web_get_contents` 一次最多 10 个 URL × 20k 字符

`_EXA_PAGE_MAX_CHARS = 20000`，`_EXA_MAX_CONTENT_URLS = 10` → 单次调用最多 200k 字符，再由 50KB 字节上限头尾截断。中间全丢，agent 常被迫重取。

### P2-1 提示词自身的冗余

- 第三部分（流程）与第五部分（分级）、第六部分（报告）互相引用同一批规则，措辞重复。
- 报告格式规范（2.2k）对 root 渲染完整版，但 root 只做汇总。
- `coordination/root_agent.md`（8.8KB / 2.4k tok）内联进 root 前缀，与 system prompt 的 `<root_agent_directive>` 语义重叠。
- `scope.jinja` 1,085 tok 每个 agent 都带，内容大部分是静态授权说明。

### P2-2 顺带发现的两个缺陷

1. `Ben/agents/profiles.py` 中 `recon` / `enum` 的 `skills=("reconnaissance",)` 是**分类名不是片段名**，`resolve_skill_name` 抛 `SkillNotFoundError` → 被 `_resolve_skills` warn-and-skip。结果是这两个 profile **静默拿不到侦察类方法论**（不影响 token，但影响效果，且每次 spawn 刷一条 warning）。
2. `tool_output_max_tokens` 只在 shell 路径生效，非 shell 工具（proxy / RAG / MCP）走 50KB 字节线，**同一份配置对两类工具含义不同**。

---

## 四、优化建议

按「账单杠杆」分三档。第一档动**乘数（轮次）**，第二档动**基数（历史）**，第三档才是**常数项（前缀）**。

### 第一档：动轮次和历史（账单杠杆最高）

| # | 措施 | 收益 | 改动面 |
|---|---|---|---|
| 1 | **压缩阈值与窗口解耦**：新增 `BEN_CONTEXT_COMPACT_AT`（建议 40~60k），不再等 `window-20k`=180k | 晚期上下文从 180k 降到 ~50k，`Σ上下文` 直接砍一半以上 | `compaction.maybe_compact`（触发线一行） |
| 2 | **工具输出统一按 token 计价**：用 `count_tokens` 替代 SDK 的 `bytes/4`，默认阈值降到 2,000~3,000 tok | 长尾 11k → 3k，历史基数 -40% | `output_store` + `factory._tool_output_limits` |
| 3 | **截断改「头 + 计数 + 指针」**：保留前 N 行 + 行数/字节数 + spill 路径 + 一句 `grep -n` 指引，**丢掉尾部** | 减少"看尾部再全量重取"，等效少 15~20% 工具轮次 | `output_store._head_tail` |
| 4 | **结论按引用传递**：`agent_finish` 只回 `report_ids[] + 一句话摘要`，正文留台账 | 多 agent 链路历史 -20~30% | `agent_finish` / `finish_scan` schema |
| 5 | **把 `max_agent_turns` 从 12 收到 8**（profile 已有 `max_turns` 机制，反之向上收紧 root） | 每少 1 轮 ≈ 省 1 份完整上下文 | `settings.max_agent_turns` |

### 第二档：动工具面（减少无效轮次 + 常数项）

| # | 措施 | 收益（计数口径） | 改动面 |
|---|---|---|---|
| 6 | **收紧 exploit/validate/sca 的 `tool_names`**：42 → 12~15 个（shell + proxy 只读 + think/notes/coverage + 本 profile 报告工具） | child 固定成本 27k → 14k（-48%） | `profiles.py`（照抄 recon/enum 范式） |
| 7 | **报告工具延迟挂载**：只在 `report_tool=True` 的 child 挂 `create_*_report` | 每轮 -5~6k（-22%） | `factory._compose_agent_tools` |
| 8 | **给 root 也做白名单**：root 占 55~65% 请求却挂 28 个工具（含 MCP 五件套 3.1k、coverage 三件套 3.2k） | 每轮 -3~4k | `factory._ROOT_*_DROP` |
| 9 | `web_get_contents`：单页上限 6k 字符、默认 3 个 URL | 最坏 200k 字符 → 18k | `web_search/tool.py` 常量 |

> 第 6~8 项在缓存生效时换算账单只值 ~10%，但**会同时减少工具误用导致的无效轮次**，所以仍值得做；真正的理由是指令密度和正确性，不是省 token。

### 第三档：动前缀和提示词

| # | 措施 | 收益 | 改动面 |
|---|---|---|---|
| 10 | **skills 改「目录 + 按需」**：前缀只留目录（393 tok），正文一律走 `load_skill`；root 仅内联 `root_agent` | child 每轮 -6~10k（-30%） | `prompt._resolve_skills` + `create_agent` 语义 |
| 11 | **提示词去重 + 报告规范按角色裁剪**：第三/五/六部分合并重复条款；root 只给"汇总 + 引用"版 | 1.5~2k/轮 | `system_prompt.jinja`（会触发文本断言测试） |
| 12 | **修缺陷**：`profiles.py` 的 `skills=("reconnaissance",)` → 真实片段名；`tool_output_max_tokens` 语义统一 | 效果修复 | `profiles.py` / `settings.py` |

**组合效果**：1+2+6 三项（阈值、输出计价、工具面）落地，单次扫描的 `Σ输入 token` 约从 2.0M 降到 **0.9~1.1M**；叠加 4+10 可到 **0.6M 左右（-70% 计数）**。账单降幅取决于缓存命中率，需用真实 run 的 `cached_tokens` 复核（见下节），不要用计数直接推账单。

**硬边界**：以上全部不动安全语义——授权闸门在 `_wrap_exec_command`，证据纪律在提示词主干，两者都不在本次改动清单里。

---

## 五、验证方式

```bash
# 固定前缀基线（改前后对比这两个数）
.venv/bin/python -c "
from Ben.llm.context_budget import count_tokens
from Ben.agents import factory as f
from Ben.agents.prompt import render_system_prompt
import json
t=sum(count_tokens('grok/grok-4.6',(x.description or '')+json.dumps(x.params_json_schema,ensure_ascii=False)) for x in f._BASE_TOOLS)
print('tools:', t)
print('root prompt:', count_tokens('grok/grok-4.6', render_system_prompt(scan_mode='deep', is_root=True)))
print('child prompt:', count_tokens('grok/grok-4.6', render_system_prompt(scan_mode='deep', is_root=False)))
"

# 历史侧：解析任一 run 的实际用量
.venv/bin/python - <<'EOF'
import sqlite3, json
from Ben.llm.context_budget import count_tokens
con=sqlite3.connect("strix_runs/<run>/.state/agents.db")
for sid,md in con.execute("select session_id,message_data from agent_messages"):
    print(sid[:8], count_tokens("grok/grok-4.6", md))
EOF
```

回归基线：单文件 pytest 通过即可（全量有约 99 个预先存在的环境相关失败，见 `.workbuddy/memory/MEMORY.md`）。改动 `system_prompt.jinja` 会触发 `tests/agents/test_root_prompt_policy.py` 等对文本断言的测试，需同步更新。

### 账单侧复核（必须做）

计数收益 ≠ 账单收益。改完后跑一次真实 run，从 `usage` 里读 `cached_tokens`（`Ben/core/orchestration_metrics.py` 与 `run.json` 已有用量字段），按

```
billed ≈ 0.1 × (Σ输入 - Σ缓存命中) + 1.0 × Σ缓存未命中 + Σ输出
```

核对。重点关注三项指标：

1. `Σ输入 / 轮次` —— 平均上下文长度（应随措施 1/2 明显下降）
2. `缓存命中率` —— 如果因为频繁改工具列表导致前缀失效，收益会被吃掉（第 6~8 项改动会让前缀变一次，属一次性成本）
3. `轮次总数` —— 最直接的乘数
