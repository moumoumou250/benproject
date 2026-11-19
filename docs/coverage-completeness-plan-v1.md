# 覆盖完整性方案 v1 —— 让"漏扫"从沉默变成可测量

> 目标：不承诺"扫完一切"（不可能），承诺**每一条被看见的面都有明确去向**。
> 适用范围：`ben` 主扫描链路（root 编排 + recon 种子 + coverage 账本 + 收尾闸门）。
>
> **状态（2026-11-19）**：P0 / P0.5 / P1 与 P3 骨架已落地；跟进完成 root 认 pending、Ready 排序、受控 bulk_close、有条件多 risk。
> 约束：无输入点仍一面一主 risk；`?`/写方法可拆至 3 个；Ready 不再被 DFS 折叠；`pick` 同层 FIFO。

---

## 1. 问题定义

"漏扫"有两种，性质完全不同：

| 类型 | 表现 | 现有机制能否发现 |
|---|---|---|
| **A. 看见了没查完** | 账本里有 `needs_follow_up`，收尾没清 | 能。`finish_scan` 会列出，但只警告 |
| **B. 根本没看见** | 面从未进入任何账本，报告里也不会写"未覆盖" | **不能。这是主要漏法** |

类型 B 才是"报告很干净但其实没查"的根源。它的成因不是"字典不够厚"，而是**管线里有多级截断，把看见的东西丢了**。

### 核心判据

> 不漏 = **枚举** × **查完** × **记录未看见**
>
> 乘法关系。任一项为 0，整体为 0。
> 加厚字典只提升第一项，且边际递减；当前最弱的是第三项——**系统没有"未看见"这个概念**。

### 术语

- **Inventory（清单）**：机器枚举出来的、客观存在的面（路径、接口、端口、表单）。来源可复核。
- **Queue（待办）**：排序后当前该查的子集。有预算上限。
- **Ledger（账本）**：每个面查完后的结论（现有 `coverage.json`）。

现状只有 Queue 和 Ledger，**缺 Inventory**。这是全部问题的结构根源。

---

## 2. 现状体检：八级漏斗

一次 `ben -t a.com --seed recon.json` 从"目标上有 300 个可达面"到"root 派出 2 个 agent"，中间经过八道截断：

| # | 漏斗 | 代码位置 | 现状值 | 后果 |
|---|---|---|---|---|
| 1 | 路径字典固定 | `recon.py:63` `_DEFAULT_PATHS` | 24 条 | 只探常见路径 |
| 2 | JS 只抓 3 个 | `recon.py:846` `same_host_scripts[:max_js]` | `--max-js 3` | SPA 接口大量泄漏在未抓脚本里 |
| 3 | 接口硬截断 | `recon.py:864` `items[:60]` | 60 条 | 长尾接口直接丢弃（`count` 记了真实数但无人消费） |
| 4 | 种子 brief 截断 | `seed.py:26` `_BRIEF_FACT_CAP=24` | 24 条 fact | `surface` 优先级高于 `endpoint`，**24 条路径刚好占满，接口/表单/端口被挤成一句"… N more"** |
| 5 | Ready 窗口 | `frontier.py:32` `_MAX_READY_ROWS=8` | 8 条 | 意图超过 8 条，剩余只显示计数 |
| 6 | Ready 被 DFS 折叠 | `frontier.py:293-301` | DFS 有 open intent 时折叠 | 广度缺口（`needs_follow_up`）细节完全不显示 |
| 7 | 端口上限 | `scout.py:305` `_MAX_PORTS_HARD` | 200 | 默认只扫 31 个常用端口 |
| 8 | 收尾不拦 | `finish/tool.py:152-172` | `success=True` + warning | 未清账照样定稿，报告不会自己写"未覆盖" |

### 关键推演

第 4 与第 5 条叠加，产生一个反直觉后果：

> **把字典从 24 条加到 500 条，root 看到的反而更少。**
>
> 因为 fact 层固定截 24 条、intent 层固定显示 8 条。
> 24 条路径时不觉得少；500 条路径时，492 条被折叠成一行"… 492 more"，而 `endpoint` 依然排不进去。
> **加厚枚举但不改可见窗口，等于把网撒大却把桶变小。**

这就是为什么"侦查再详细一点"单独做无效。

### 第九道漏斗：排序退化（本节新增，实为最隐蔽的一道）

`task_graph.pick()` 的排序键是 `(-depth, -created_at)`，名义上 DFS。但 `_depth_locked` 靠 `fact.parent_fact` 链累加，而 `record_fact` 的 `parent_fact` 是**可选参数、默认 `None`**（`Ben/tools/facts/tools.py:28`）。侦察种子与绝大多数 agent 写入的 fact 都没有父节点，深度全为 0，排序退化成：

> **`created_at` 降序 = 后写的排前面 = 一个 LIFO 栈**

后果比 DFS 更糟：DFS 至少会沿一条链走完，LIFO 会被**每一次新发现**打断。agent 只要持续报新接口，老面（`/admin`、`/upload`）就永远沉在栈底，且因为 Ready 只看前 8 条，它们连"被看见"的机会都没有。

补充事实：能派生 intent 的 fact 只有 `host` / `service` / `dir` / `endpoint`（`task_graph.py:31-36`），`vuln` / `cred` / `note` **不派生任务**。所以"发现漏洞就自动再挖一层"并不存在，往下钻只发生在枚举链上。

**这一条决定了 P2 的优先级应高于原计划**：把 `pick()` 改成"同层未清完不翻层 + 同层老优先"（约 20 行），即使 `parent_fact` 依然缺失也有效——因为此时全部深度为 0，等价于"全体按发现顺序 FIFO"，恰好就是"先把侦察兵交回来的清完"。

---

## 3. 目标模型

```
                 ┌─────────────────────────────────────────┐
  recon/scout ──▶│  Inventory（新增）                       │
                 │  机器枚举 · 带来源 · 只增不改             │
                 │  surface, kind, discovered_by, evidence  │
                 └───────────────┬─────────────────────────┘
                                 │ 初始化时写入 coverage(pending)
                                 ▼
                 ┌─────────────────────────────────────────┐
                 │  Ledger（现有 coverage.json，扩一种状态） │
                 │  pending → reported / ruled_out /        │
                 │            no_issue_found /              │
                 │            not_applicable /              │
                 │            needs_follow_up（试过没成）    │
                 └───────────────┬─────────────────────────┘
                                 │ frontier 渲染
                                 ▼
                 ┌─────────────────────────────────────────┐
                 │  Queue（现有 Ready + DFS）                │
                 │  按剩余预算动态决定可见条数               │
                 └───────────────┬─────────────────────────┘
                                 ▼
                 ┌─────────────────────────────────────────┐
                 │  Gate（finish_scan，改为硬闸门）           │
                 │  pending 未清 → 拒收 / 或强制写入未覆盖章节 │
                 └─────────────────────────────────────────┘
```

### 三处关键设计决策

**决策 1：`pending` 加在 coverage 里，而不是新建一张表。**

- 理由：join 天然完成，`resume` 只 hydrate 一次，报告侧只读一处。
- 代价：要同步改 `VALID_OUTCOMES`、`OUTCOME_LABELS`、SARIF 映射、`unresolved` 判定。改动点集中，可控。
- 语义区分必须严格：
  - `pending` = **没试过**（枚举进来，无人认领）
  - `needs_follow_up` = **试过，没结论**（缺凭据、缺服务）
  - 两者在报告里文案不同，不可混用。

**决策 2：`pending` 不是 agent 工具，只有运行时能写。**

- `record_coverage` 的 5 种 outcome 保持原样，agent 不能"登记待办"来刷数字。
- agent 关闭 pending 的唯一方式：拿到 `entry_id` 后 `update_coverage(entry_id, ...)`。
- 因此 frontier 的每条建议必须带 `entry_id`，否则 agent 只能新建重复行（现有查重会拒绝，白费一轮）。

**决策 3：收尾闸门分两档，默认开"软"、可切"硬"。**

- `BEN_COVERAGE_GATE=warn`（默认过渡期）：warning + 报告自动生成"未覆盖清单"章节。
- `BEN_COVERAGE_GATE=strict`（推荐长期）：pending 未清 → `success=False`，返回待清列表，root 必须派人或 `update_coverage` 标 `not_applicable`。
- 为什么不一上来就 strict：存量扫描会被大面积打断，先让数据跑一轮看清基数。

---

## 4. 分期实施

### P0 — 打通账本（最小闭环，先做这个）

**目标**：recon 探到的每一面，自动成为账本里的 `pending` 行；收尾时逐条交代。

| 改动 | 文件 | 内容 |
|---|---|---|
| P0-1 | `Ben/tools/coverage/tools.py` | `VALID_OUTCOMES` 加 `pending`；`pending` 纳入 `_OUTCOMES_REQUIRING_EVIDENCE`（必须写来源）；新增程序化 API `seed_pending_entries(items: list[dict]) -> int`，幂等（同 surface+risk 已存在则跳过） |
| P0-2 | `Ben/core/inventory.py`（新建） | 从 `recon.json` / `scout.json` 解析 inventory：`kind=surface` 且 `status ∈ {200,201,204,301,302,401,403,405}` 且 `fallback != true` 且 `baseline != true` → 存活面；`kind=endpoint` 的 items → 待验接口；`kind=service` 有 port → 服务面。按路径语义推断 `risk_area`（`/login|/admin` → `auth`，`/api/*` → `authz`，含 `?` → `xss,sqli`，无则 `enum`） |
| P0-3 | `Ben/core/runner.py`（hydrate 之后） | 调 `seed_pending_entries`，把 inventory 灌进账本；日志打印 `inventory: N surface(s) seeded as pending` |
| P0-4 | `Ben/core/frontier.py` | `_coverage_gaps` 纳入 `pending`；Ready 段**不再因 DFS 非空而折叠**（改为：DFS 在前，Ready 紧随，各占预算）；每条建议追加 `coverage_entry_id` |
| P0-5 | `Ben/tools/finish/tool.py` | `_coverage_summary` 区分 pending / needs_follow_up；`BEN_COVERAGE_GATE=strict` 时返回 `success=False` + 待清清单 |
| P0-6 | `Ben/report/coverage.py` | `OUTCOME_LABELS` 加 `pending`；报告新增「Not covered」章节，列未清项与来源 |

**验收**：

1. 对任一目标跑 `ben recon` 生成种子，再 `ben -t <target> --seed <recon.json>`。
2. `_artifacts/runs/<run>/coverage.json` 中 `outcome=="pending"` 的条数 == recon 判定的存活面数（可对比 `recon.json` 的 `facts` 自行核算）。
3. 扫描结束后 `finish_scan` 的返回体里出现 `unresolved_surfaces`，且报告中含「Not covered」章节。
4. `BEN_COVERAGE_GATE=strict` 时，若不处理 pending，`finish_scan` 返回 `success=False`。

**预计改动量**：约 350–450 行（含测试）。

---

### P1 — 加厚枚举（让 Inventory 更真）

P0 打通后，加厚才有意义。顺序按投入产出排：

| 改动 | 文件 | 内容 | 收益 |
|---|---|---|---|
| P1-1 | `seed.py:26` | `_BRIEF_FACT_CAP` 改为**按 kind 配额**：surface 8 / endpoint 12 / service 8 / param 4 / note 4（共 36），而不是单一 24 条按优先级抢 | **最高**。解开"24 条路径挤掉所有接口"的死结 |
| P1-2 | `recon.py:864` | `items[:60]` → `items[:300]`，并把 `count` 与 `shown` 同时写入，缺口显式可见 | 高，且零风险 |
| P1-3 | `recon.py` 参数 | `--max-js` 默认 3 → 15（脚本数，不是字节数）；新增 `--js-budget-kb` 防止单文件过大 | 高。SPA 接口主要来源 |
| P1-4 | `recon.py` 字典 | `--paths` 默认 24 → 200（内置分层字典：`common`=24 保持快，`standard`=200，`deep`=2000 需显式指定）；新增 `--path-tier` | 中。收益递减明显，但 `standard` 档值得 |
| P1-5 | `scout.py` | 新增 `--port-tier {common,top1000,full}`，默认 `common`（31）；`top1000` 需显式开启，且 `_MAX_PORTS_HARD` 按档位调整；`-sV` 版本识别仍限 16 个 | 中。侧门收益真实但耗时大 |
| P1-6 | `recon.py` | 表单解析补 `action` + `method` + 字段名，输出为独立 `form` fact（现为 `param` 混装） | 中。直接决定 authz/sqli 的可测性 |

**注意**：P1 必须**在 P0 之后**。否则枚举变厚、可见窗口不变，纯属浪费请求。

**验收**：同一目标，`recon.json` 的 `counts.endpoint` 提升 ≥3 倍；种子 brief 中同时出现 `surface` 与 `endpoint` 两类行（当前 endpoint 必然被挤掉）；`coverage.json` 的 pending 条数随之上升，且全部能在收尾时逐条交代。

---

### P2 — 调度与预算（让"深/浅"名副其实）

| 改动 | 文件 | 内容 |
|---|---|---|
| P2-1 | `Ben/interface/cli/*`（`-m` 解析处） | 档位真正改资源：quick = turns 8 / 并发 2 / 子 agent 25min；standard = 12 / 4 / 45；deep = 24 / 6 / 90，并给 `BEN_SCAN_MINUTES` 默认值（如 45/90/180） |
| P2-2 | `frontier.py:32` | `_MAX_READY_ROWS` 动态化：按剩余 turn 与 pending 数计算（如 `min(12, max(3, 剩余turn))`），不再是常数 8 |
| P2-3 | `task_graph.py:382` | `pick()` 支持 `BEN_BREADTH_FIRST=1`：深度升序（先清同层全部兄弟面），默认在 `scan_clock` 进入 endgame 前保持关，endgame 后自动切广度 |
| P2-4 | `core/task_claims.py` | claim 键加 `angle`：`(surface, risk_area, angle)`；冲突时返回"可换 angle 重试"而非硬拒，与 `root_agent.md:83` 的"异角度开第二个 child"对齐 |
| P2-5 | 环境默认 | `BEN_CLAIM` 默认 `0 → 1`（需先验证对并发根因无副作用） |

**验收**：`-m quick` 与 `-m deep` 的运行日志中 `max_turns` 不同；同一目标 deep 模式的 pending 清理率显著高于 quick；`BEN_BREADTH_FIRST=1` 时 `pick()` 返回深度最小的 ready intent（单测覆盖）。

---

### P3 — 度量（让"防漏"变成数字）

没有这个，前面三期都只是"感觉更全"。

| 改动 | 内容 |
|---|---|
| P3-1 | 建靶场回归集：Juice Shop（含登录后与隐藏路径）、DVWA、WebGoat，各维护一份**已知漏洞清单**（漏洞类型 + 位置 + 应被谁检出） |
| P3-2 | 新增 `ben eval --suite <name>`：跑完对照清单，输出 `recall`（检出/应有）、`false_positive`、`uncovered_surfaces` |
| P3-3 | 把 recall 写进 CI 基线；改动 P0–P2 时对比，防止"改好了可见性、改坏了检出率" |

**验收**：`ben eval --suite juice-shop` 输出 recall 数值；连续两次运行波动 < 5%。

---

## 5. 取舍与不做什么

| 不做 | 理由 |
|---|---|
| 追求"扫完所有面" | 攻击面是 状态 × 参数 × 角色 的组合，无限；追求它只会让预算摊薄、报告变水 |
| 默认全端口扫描 | 65535 端口耗时数十分钟、噪声大、对纯 Web 目标收益低。提供 `--port-tier full` 让用户显式选 |
| 默认超大字典 | 5000 条路径 ≈ 83 分钟（1 秒/条），且极易触发 IP 封禁，后续什么都查不了 |
| 把 pending 做成 agent 工具 | agent 会用它"登记工作量"来显得勤快，污染账本 |
| 引入 tmux / 进程隔离 | 子 agent 已是独立上下文，搬过来只增加跨进程状态与消息丢失面 |
| 自动 brute force 凭据进三楼 | 越权且高风险。三楼的正解是**用户提供测试账号**，无账号则如实写 `not_applicable(no credentials)` |

---

## 6. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| pending 数量爆炸（200 路径 → 数百条） | 报告冗长，root 面对长清单可能"批量标记 not_applicable" | 报告侧按 `risk_area` 聚合展示；`not_applicable` 强制 evidence，且计入 `oracle_gap` 统计供人工抽查 |
| strict 闸门打断存量流程 | 已有自动化脚本失败 | 默认 `warn`，strict 需显式开启；提供 `--allow-open-coverage` 逃生阀 |
| 枚举加厚引发目标封禁 | 后续测试全部失败 | P1 加 `--rps` 限速（默认 5）；遇 429/403 连续 N 次自动降档并记录 |
| 加厚后预算被侦查吃掉 | 漏洞测试时间减少 | P2-1 让档位带总时长；P2-3 在 endgame 前强制切广度 |
| 与 resume 冲突 | 重跑时 pending 重复灌入 | `seed_pending_entries` 必须幂等（surface+risk 去重），并有单测 |

---

## 7. 一句话总结

**现在的系统能保证"我查过的都记了"，不能保证"该查的都查了"。P0 把"该查的"变成账本里的一行，P1 让这行更真，P2 让这行有人查，P3 让漏了多少变成数字。**

---

## 8. 实现进度对照（2026-11-19 核查）

### 已落地（质量可用，测试 16 + 55 全绿）

| 项 | 位置 | 说明 |
|---|---|---|
| Inventory 抽取 | `Ben/core/inventory.py`（184 行） | 存活判定严格：排除 `fallback`/`baseline`、状态码白名单；endpoint/service 均抽 |
| `pending` 状态 | `tools/coverage/tools.py:41` | agent 禁写（`_AGENT_FORBIDDEN_OUTCOMES`）；`seed_pending_entries` 幂等 |
| 启动接线 | `core/runner.py:822-833` | hydrate 后灌入，异常非致命 |
| Ready 改造 | `core/frontier.py:138-160, 304-325` | pending 进 Ready、带 `entry_id`、不再被 DFS 折叠 |
| 收尾闸门 | `tools/finish/tool.py:98-101, 140-213` | `BEN_COVERAGE_GATE=strict` 拒收（默认 warn） |
| 报告章节 | `report/coverage.py:333-354` | 「Not covered」按 risk_area 聚合 |
| brief 配额 | `cli/seed.py:27-37` | 按 kind 配额共 44 条（surface 8 / endpoint 12） |
| recon 加厚 | `cli/recon.py` | standard 档 192 条路径、`--max-js 15`、`--js-budget-kb 512`、endpoint 300 |
| 排序 | `core/task_graph.py:394-405` | 同层老优先；`BEN_BREADTH_FIRST` 可切（**默认仍 DFS**） |

### 本轮已补（评估后跟进）

| 项 | 说明 |
|---|---|
| root 认 `pending` | `root_agent.md` + `finish_scan` 对账文案；`pending` = 系统派活 |
| Ready 排序 | `ready_sort_key`：非静态 → auth/authz/注入 → 401/403 → 动态 200 |
| 受控批量关账 | `bulk_close_coverage`：仅 pending→`not_applicable`，强制 evidence，报告 `bulk_waivers` |
| 有条件多 risk | 仅 `?` / 写方法面附加 xss/sqli（最多 3） |

### 仍未落地（按优先级）

| # | 缺口 | 后果 | 建议 |
|---|---|---|---|
| 4 | `_MAX_READY_ROWS = 8` 仍为常量 | 入账越多，越看不到中后段 | 按剩余 turn 动态；或分页 |
| 5 | `BEN_BREADTH_FIRST` 默认关 | 一旦有 `parent_fact`，又回到钻深 | 默认开，或 endgame 前自动切 |
| 6 | `-m quick/standard/deep` 仍未改预算 | deep 与 quick 资源相同 | 档位绑定 turns / 并发 / 分钟 |
| 7 | claim 无 `angle` 维度 | 同面双角度靠命名自律 | `(surface, risk_area, angle)` |
| 9 | 端口分档未做 | 仍 31 个 + 硬上限 200 | `--port-tier` |
| 10 | 靶场黄金清单 / CI 基线 | `ben eval` 骨架已有，缺真实 recall | 填满 Juice Shop suite 并跑通 |
