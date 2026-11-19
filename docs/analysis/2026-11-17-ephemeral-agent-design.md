# 方案 A 细化设计：临时派生、用完即销毁的按需 Agent（Ephemeral Per-Node Agent）

日期：2026-11-17
对应需求：需求 1（初始阶段仅保留端口扫描 + 目录枚举，关闭全量预加载）
选型：A1（静态 profile 工具面）的"一次性 agent"落地形态

---

## 一、核心理念

**一个 DFS 节点 = 一个一次性子 agent。**

调度器（B1 的 DFS 栈队列）pop 出一个节点时，按节点类型以最小 profile 派生一个子 agent；agent 只干这一件事，把产出写进共享账本（coverage / visited 哈希 / 新节点入队）后立即 `agent_finish` 销毁。下次需要同类工作，再派生一个新的。

一句话原则：**记忆在账本，不在 agent。** Agent 是易耗品，状态全部外置——这正好契合 Ben 现有的模块级账本架构（coverage / claims / todos 本来就是进程内共享 + 磁盘双写），临时 agent 销毁不丢任何数据。

与现状的区别：现状子 agent 是"长命多面手"（全量 43 工具 + 长任务简报，自己决定做什么）；本方案是"短命终专一兵"（2~4 个工具 + 单一节点任务，做完即死）。

---

## 二、生命周期设计

```
DFS 队列 pop 节点 (node_type, host, port, path)
        │
        ▼
① 生成 SpawnSpec {profile, task_payload, parent_node_id, budget}
        │
        ▼
② spawn_child_agent(profile=RECON)   ← 走现有 execution.py:499-712 链路
        │  构建时只挂 profile 工具（见 §三）
        ▼
③ 执行：exec_command 跑 nmap/ffuf → 结果写账本
        │  - record_coverage(surface, risk_area, outcome)
        │  - 新发现路径 → 过 visited 哈希去重 → push 为子节点（DFS 栈）
        ▼
④ agent_finish(summary)              ← 现有 finish 工具，child 已挂
        │
        ▼
⑤ 销毁与释放（全部复用现有机制）：
   - coordinator.set_status(finished)          core/agents.py
   - release_for_agent 释放 claims              core/agents.py:386-398
   - 并发槽位归还（BEN_MAX_SUBAGENTS 计数 -1）  execution.py:549-567
   - session / 上下文整体丢弃，不进 compaction
        │
        ▼
⑥ 调度器 pop 下一节点（DFS 栈 → 最深优先）
```

关键点：**③ 的产出回填必须先于 ④ 的销毁**。agent 的任务简报里硬性要求"先 record_coverage / 入队新节点，再 agent_finish"，`agent_finish` 的校验逻辑（现有 `_finish_tool_use_behavior`，factory.py:1249-1256）可加一道检查：有未落账的产物则拒绝 finish 并提示补写。

---

## 三、Profile 定义（按需工具面）

`build_agent` 增加 `profile` 参数，`_BASE_TOOLS` 拆分为 profile 集合（裁剪方式沿用现有 `_ROOT_NETWORK_DROP` 模式）：

| Profile | 挂载工具 | 用途 | 典型生命周期 |
|---|---|---|---|
| `RECON_PORT` | `exec_command`（白名单收窄至 `nmap/naabu`）+ `record_coverage` + `agent_finish` | 端口扫描节点 | 1~3 轮 |
| `RECON_DIR` | `exec_command`（白名单收窄至 `ffuf/gobuster`）+ `record_coverage` + `agent_finish` | 目录枚举节点 | 1~3 轮 |
| `PROBE`（后续） | 上述 + `repeat_request`（D2 summary 档） | 单端点探测 | 3~8 轮 |
| `EXPLOIT`（后续） | 接近全量，按 risk_area 再裁 | 漏洞验证 | 长任务，可豁免"用完即毁" |

- **schema 成本**：RECON 档 3 个工具 vs 现状 43+4 个，每轮 prompt 的工具 schema token 下降一个数量级。
- **上下文不累积**：agent 只活一个节点，历史始终只有几轮，永远不会触发 compaction；跨节点的"已测过"信息由账本 + frontier 注入承载，不靠 agent 记忆。
- shell 白名单收窄实现：现有 `_wrap_exec_command`（factory.py:824-860）已做授权校验，把 `_NET_CLIENT_RE` 改为按 profile 选正则即可。

---

## 四、与现有代码的对接点（改动清单）

| 改动 | 位置 | 说明 |
|---|---|---|
| 新增 profile 参数与工具集映射 | `Ben/agents/factory.py`（`_BASE_TOOLS` / `build_agent`） | 拆 `_BASE_TOOLS` 为 `_PROFILES: dict[str, tuple[Tool, ...]]`，默认保持现状不破坏兼容 |
| profile 透传 | `core/execution.py` `spawn_child_agent()` + `tools/agents_graph/tools.py` `create_agent` | SpawnSpec 带 profile，现有闸门（并发/残局/claims）原样复用 |
| 销毁后回收确认 | `core/agents.py` `release_for_agent`（386-398） | 已有，确认临时 agent 的所有退出路径（finish/异常/超时/stop）都会走到 |
| 产出先落账校验 | `factory.py` `_finish_tool_use_behavior` | finish 前检查本 agent 是否有未记录的产物 |
| 白名单按 profile 收窄 | `factory.py` `_wrap_exec_command` / `_NET_CLIENT_RE`（401-407） | profile → 允许的二进制集合 |

**不需要动的**：runner、coordinator、compaction、proxy 工具、MCP 派发——全部原样。

---

## 五、优缺点与风险

**优点**
- token 最省：schema 最小化 + 上下文永不累积，单次节点成本接近理论下限。
- 故障隔离：一个 agent 跑偏/卡死只影响一个节点，销毁重派即可，不会污染兄弟任务。
- 可观测、可复现：每个节点一次 spawn，日志与 coverage 记录一一对应。
- 天然支持断点续扫：状态全在账本，进程重启后从队列继续派生即可。

**缺点 / 风险**
- **spawn 开销**：每个节点都要重建 agent + 首消息注入（shared findings index、任务简报）。对策：节点任务简报模板化、控制在 1KB 内；同 host 的相邻节点允许"一兵多任"（一个 RECON_DIR agent 领同父节点的 N 个兄弟目录）作为优化档。
- **冷启动知识缺失**：临时 agent 没有历史经验。对策：现有 spawn 前 recall（`core/recall.py`，token 重叠 top-3、≤800 字符）已经解决"把相关笔记喂给新 agent"，直接复用。
- **完成判定变难**：短任务要求"产出结构化、可机器校验"，否则 finish 校验形同虚设。对策：RECON 档任务简报里给出明确的完成条件（如"ffuf 结果已逐条 record_coverage"）。
- 深度优先 + 用完即毁意味着**同一工具的重复初始化**（如每次重新跑 nmap 全端口段）。对策：节点粒度设计时让"一次调用产出一批结果"（一次 nmap 覆盖全端口），而不是一个端口一个节点。

---

## 六、落地步骤

1. factory 加 `_PROFILES` 与 `profile` 参数（RECON_PORT / RECON_DIR 先行），默认行为不变 —— 兼容回归。
2. `create_agent` / `spawn_child_agent` 透传 profile；任务简报模板化（含完成条件与"先落账再 finish"硬约束）。
3. `_NET_CLIENT_RE` 按 profile 收窄。
4. finish 校验加"产出已落账"检查。
5. 单测：profile 工具集正确性、finish 校验、销毁后 claims/并发槽释放。
6. 与 B1（DFS 队列）联调：队列 pop → SpawnSpec → 回填 push 子节点。

**验证指标**：单次 recon 节点的平均 token 消耗（对比现状长命 agent）、schema token 占比、每节点 spawn 开销（毫秒级）、finish 拒绝率。

---

## 七、落地评审（2026-11-17）：A 已实现，需求 2 / 3 方案相应修正

实测代码：`Ben/agents/profiles.py`（ProfileSpec/ToolkitCache/结构化出口）、`Ben/core/task_graph.py`（fact/intent 图 + DFS pick + 哈希去重）、`Ben/tools/facts/`、factory/execution/frontier/runner/agents_graph/coverage/seed 接入改动；新增 14 测 + 回归子集 171 测全过。

### 7.1 需求 2（DFS 队列）：方案从「B1 待建」收敛为「B1 已建骨架 + 补强」

- **实现形态优于原设计**：原 B1 设想"显式 URL 树 + DFS 栈"；实际落地为 Sapper 同构的 **fact/intent 双层图**——树由 `parent_fact` 链隐含，DFS 由 `_depth_locked()` 排序（深度降序、同深取新）实现，无需维护显式栈。闭环已通：seed → depth-0 fact → 只派生 recon/enum intent → frontier `## Next (DFS order)` → `create_agent(intent_id=…)` → spawn 时 `claim_intent` → `agent_finish(result_json)` 落新 fact → 再派生 intent。
- **方案变化点 1（顺序仍是建议性的）**：`pick()` 目前无调用方，root 仍可自由 spawn（等于 B1 骨架 + B2 渲染建议的混合态）。要把"替换默认调度"做实，二选一：
  - **强制档（推荐）**：recon/enum 阶段，`create_agent` 不带 `intent_id` 时由调度器代领队首（`pick()` 接入 `spawn_child_agent`），root 只能领、不能挑；
  - **灰度档**：保留建议性，仅统计"root 领取顺序 vs DFS 序"的命中率，达标后再切强制。
- **方案变化点 2（claimed 泄漏）**：`reopen_intent` 只挂在 spawn 失败路径；agent 超时/崩溃/被 stop 而 未走 `agent_finish` 时 intent 永远卡在 `claimed`。需补 reaper：coordinator 对该 agent 终态转换时按 `metadata.intent_id` 自动 `conclude_intent(status="abandoned")` 或 `reopen_intent`（task_claims 有 TTL，task_graph 目前没有——挂在状态钩子上比加 TTL 更贴合"临时 agent 用完即毁"语义）。
- 合批（`batch_sibling_intents`，同父同 profile ≤5）与 `max_turns` 收紧（recon 8 / enum 12）已按设计落地，"一兵多任"风险已消化。

### 7.2 需求 3（全局哈希去重）：方案从「推荐 C2」修正为「保留 C1 + 双账本分工」

- 实际落地是 **C1（独立哈希表）** 而非原推荐的 C2（扩展 coverage）：`_dedupe_index: hash → fact_id`，SHA-1 身份键 + `canonical_url()`（去追踪参数、去默认端口、去尾斜杠、参数排序），持久化于 `task_graph.json`，模式对齐 coverage/claims。**已建已测，不回迁 C2**。
- 双账本分工就此明确：**task_graph = 发现身份去重**（"这个 URL/端口/路径是否已知"），**coverage = 面结论**（"这个 surface×risk_area 测到什么程度"）。`record_coverage` 成功也 `mark_structured_exit`，跨账本出口已打通。
- **方案变化点 3（双建议源竞争）**：frontier 现在同时渲染 `## Ready`（coverage gaps，旧的价值调度残留）与 `## Next (DFS order)`，root 有两套可领列表。DFS 段有内容时应把 Ready 段降级（折叠为计数或隐藏），否则"替换默认调度"在提示词层面不成立。
- 可选加固：`dir` 的身份键目前是 `("url",)`，`canonical_url` 未做路径大小写/百分号编码归一（`/Admin` vs `/admin`、`%20` vs 空格）。对目录枚举场景建议加**可配置的** lowercase+decode 归一（默认关，因为文件系统大小写敏感的目标会误判去重）。

### 7.3 修正后的待办清单（按优先级）

1. intent reaper：终态钩子自动 conclude/reopen（堵 claimed 泄漏，否则 DFS 队列会越跑越窄）。
2. frontier 建议源合并：DFS ready 非空时降级 Ready 段。
3. `pick()` 接入 spawn（强制档）或先做命中率统计（灰度档）。
4. `canonical_url` 可选路径归一（默认关闭）。
5. 验证矩阵补两例：stop_agent 路径的 intent 释放、resume 时 claimed intent 的启动期回收。

