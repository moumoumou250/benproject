# 台账体系优化方案

> 26 本 → 4 本业务台账。本文件是唯一执行入口，三轮分析的结论已收敛到此，不必再翻原文。

**状态**：方案定稿（v2，含实测校正） · **依据**：`docs/ledger-audit-v1.md`（盘点）、`docs/sapper-ledger-comparison-v1.md`（对照）、`docs/ledger-bench-v1.md`（实测）

---

## TL;DR

| 指标 | 现状 | 目标 |
|---|---|---|
| 业务台账数量 | 7 本（+19 本投影/基础设施） | **4 本** |
| 单次图写入延迟 | 1k facts 时 **51 ms**，且全程持锁 | **≤ 5 ms，与规模无关** |
| 写放大 | **500×**（千写 395 MB） | 常数（≈ 写入量本身） |
| 台账工具静态 token | **3,791 tok/请求**（占 child 静态输入 28.9%） | **2,208 tok/请求**（−42%） |
| coverage 实际产出 | 11/11 可读 run 全部 `entries=0` | 机器观测自动填充，agent 只纠偏 |

---

## 一、为什么会变成 26 本

台账数量 ≈ **读者种类 × 写入时机种类**。Sapper 把两者都压到 1（读者=读图的 agent，写入=CLI 调用），所以只有 1 本；我们五个成因各自展开了一次：

1. **事故驱动增量**——claims / verify / coverage / todos 是四次真实故障的化石，每次都是"加一本"而不是"加一个字段"。
2. **进程内 asyncio 多 agent** 需要精确恢复 → agents.json / agents.db / progress / claims 四本。
3. **产品化契约**——run.json、SARIF、执行报告、usage，竞赛 harness 一本都不需要。
4. **把视图当数据存**——最没道理的一类，视图因此有了 schema 和版本迁移。
5. **没有统一图作为收敛点** → 长出 5 套并行去重机制。

**结论：问题不在"本数多"，在于没有单一写入点。** 所以本方案不追求"像 Sapper 一样只有 1 本"，而是先造出那个收敛点。

---

## 二、判据（唯一一条）

> **只有「独立的写入时机 + 独立的 schema」才配独立成一本台账。**
> 能计算出来的 → 视图；能放进某个字段的 → 字段。

用这条判据过一遍，26 本里 19 本不成立。

---

## 三、目标形态：4 本 + 3 件存档

| # | 台账 | 承担 | 谁写 | 为什么不能省 |
|---|---|---|---|---|
| 1 | `task_graph`（facts / intents） | 唯一调度骨架：发现了什么、该做什么、谁在做（`intent.worker`+TTL）、试过几次（`attempts`）、验证结论、最终 `outcome` | agent 工具 + 代码自动（seed / agent_finish 回灌） | 它就是那个收敛点。待办、认领、覆盖、尝试次数全部是它的字段或视图 |
| 2 | `vulnerabilities.json` | 唯一权威发现源，含 `verification_status` | agent 工具 | 可变对象、有生命周期（draft→reported→verified/refuted）、要出 SARIF、要人审。放进不可变图会脏图 |
| 3 | `threat_models.json` | 方法与视角（威胁建模、严重度校准） | agent 工具 | 是方法不是状态，提示词多处依赖，没有替代物 |
| 4 | `notes.json` | 唯一非结构化记忆（methodology / questions / plan），**禁 findings** | agent 工具 | 自由文本草稿不能伪装成可判定事实；Sapper 也保留 `hint` 通道 |

**3 件会话存档**：`run.json`、`agents.json`、`agents.db`。**不计入台账**——它们是 resume 与审计凭据，不参与"合并语义"的讨论，本方案一律不动。

---

## 四、26 本的逐项去向

### 降为派生视图（不落盘）

| 台账 | 去向 |
|---|---|
| `coverage.json`（运行级 + `.state`） | 从 open/abandoned intents + 工具边界观测 + 未覆盖 surface **现算**；`ben board` 统一渲染 |
| `todos.json` | intents 的只读渲染视图 |
| `vulnerabilities/<id>.md`、`.csv` | finish / view 时渲染 |
| `findings.sarif`、`penetration_test_report.md` | 保留为**交付物**，但只在 finish 时生成一次 |

### 内联为字段

| 台账 | 去向 |
|---|---|
| `claims.json` | `intent.worker` + `intent.claimed_at` + `intent.ttl`（226 行代码退场） |
| `verify_queue.json` | `vuln.verification_status` + `vuln.verification_note` |
| `notes[findings]` | 并入 vulnerabilities 的 draft 状态 |

### 并入日志或删除

| 台账 | 去向 |
|---|---|
| `heartbeat.jsonl` | 未启用 → 到期未用则删（见 P5） |
| `orchestration_metrics.json` | 同上，或并入 `ben.log` 结构化行 |
| 根 `skills/`（100 KB，代码不加载） | 直接删，不迁移 |
| `~/.ben/mcp-servers.json` | 文件不存在，登记项从文档里删 |

### 不动

`run.json`、`agents.json`、`agents.db`、`threat_models.json`、`findings.sarif`（作为交付物）、`recon/`、`reports/`（仅把人工评审快照移出）。

---

## 五、执行顺序

> 顺序由实测决定，不是由"看起来合理"决定。**P0 与 P1 相互独立，可并行**；P2 之后严格串行。

| 阶段 | 动作 | 依据（实测） | 预期 | 验收 |
|---|---|---|---|---|
| **P0** | task_graph 存储改为 **snapshot + WAL**：写 = 追加一行到 `wal.jsonl` + fsync（O(1)）；读 = `snapshot.json` + 重放 WAL 尾部；每 500 条或 run 结束 compact 一次。**不要用一节点一文件**（见 §六-7） | 单文件整写 51 ms/写、500× 放大；同时实测一节点一文件读 1000 节点 **426 ms**（open 0.34 ms/个），比单文件 2.9 ms 退化 **147×** | 写 **1–2 ms 常数**；读 **≤ 8 ms**，均与规模无关 | 1k facts 写 ≤ 5 ms、冷加载 ≤ 10 ms；并发 8 写无长尾 |
| **P1** | 工具面瘦身：删 `record_coverage`(−751)；todo 6→3(−519)；notes 5→3(−313) | 三组 schema **3,791 tok/请求 = 28.9% 静态输入** | **−1,583 tok/请求**（台账工具面 −42%，总静态输入 −12%）；200 请求/run ≈ 省 32 万 tokens | 静态输入 ≤ 11.5K；功能单测仍 52 passed |
| **P2** | coverage 机器化：**工具边界埋点** → `coverage_observed.jsonl`（append-only），`agents.db` 兜底；agent 只调 `update_coverage` 纠偏 | `run.json.tool_calls` **恒为 0**（原方案数据源判断错误）；agents.db 解析 1.3 ms | 覆盖从 0 变真实；`list_coverage` 可并入 board | 一次真实 run 的 entries > 0 且与 agent 实际动作可对账 |
| **P3** | 待办单轨：claims 内联 intent（TTL/防抢）；todos 工具**先双写** task_graph→确认无回退→再降只读 | todo 写 4 ms、50× 放大；语义与 intents 重复 | 一套状态机；`BEN_CLAIM` 退役 | 无人再读 `todos.json`；旧格式兼容读 |
| **P4** | 发现单源：notes 禁写 `category=findings`（工具侧拒绝）；verify 内联 `vuln.verification_status` | vuln 写 8–10 ms 可接受 | 一条发现一个权威源 | 全库只有 1 处写发现 |
| **P5** | UID 可读性（容器固定 UID/GID 或出口 `chown -R`）+ 卫生（删根 skills、清理文档、潜伏台账到期决策） | **9/20 run 产物 `nobody:nogroup`，宿主侧连 root 都读不了** | 台账"写了读得了" | 全部历史 run 目录可读；无 0 使用台账 |
| **P6** | baseline / reconcile：baseline = 应扫全集，gap = baseline − observed − conclusions | 前置就绪 | 防漏扫 | gap 报告可重现 |

**已降级**：投影降频——一次完整 `_write_artifacts()` 仅 **16–17 ms**，100 次才 1.7 s/run。原方案高估了 IO，改为顺手项，不单独排期。

---

## 六、硬边界（照抄执行会出问题）

1. **P0 之前不要合并任何台账**。原计划先合 claims/todos 进 task_graph，但单文件 1k facts 时已 51 ms 持锁 —— 先合并会让整条链路更慢。
2. **机器推导的 coverage 初始 outcome 只能是 `needs_follow_up` / 未定，绝不自动落 `no_issue_found`**。"跑过一条命令" ≠ "对这个面有结论"。假 coverage 会污染 frontier 排除集和 SARIF 的 `pass`，**比空转更危险**。
3. **fact 与 coverage 不合并**（身份去重 ≠ 面结论）；**notes 与 vulns 不合并**（可判定事实 ≠ 自由文本）。
4. **不动 resume 链与 vuln 权威源**。run.json / agents.json / agents.db 是产品契约。
5. **空转修好前不上 baseline**，否则变成第 5、第 6 本业务台账。
6. **不为删死代码做迁移**。根 `skills/` 100 KB，删就完了，迁移成本大于它本身。
7. **不要把 Sapper 的"一节点一文件"直接套用**。实测 1000 节点冷加载 **426 ms**（open 0.34 ms/个，overlayfs），单文件 133 KB 仅 2.9 ms —— 写快了但读废了，退化 147 倍。Sapper 能这么做是因为它只有几十到几百节点；我们长跑会到几千，必须 WAL + snapshot。
8. **改工具必须同步改提示词**。P1 删掉 `record_coverage` 后，若 `Ben/skills/` 里的指引片段还在提它，模型会调用不存在的工具 → 报错 → 重试 → token 反而上升。这一步的净收益完全取决于同步删除。

---

## 七、防复发：新增台账的准入四条

以后任何"想加一本"的提案，必须同时满足：

1. 它有**独立的写入时机**（不能挂在已有台账的写路径上）；
2. 它有**独立的 schema**（不能是某个字段能表达的）；
3. 它有**唯一的读者**，且该读者不能接受视图渲染；
4. 它能说清"如果不存在，哪一步会错"。

四条缺一，就做成字段或视图。**这一节比前面所有阶段都重要**——不设这道闸，两年后又是 26 本。

---

## 八、实测基线（复核用）

| 项 | 数值 |
|---|---|
| 原子写 fsync 地板 | ~4 ms（1 KB 3.77 ms → 2 MB 17.99 ms） |
| task_graph | 100 facts 8.6 ms / 300 22.7 ms / 600 43.3 ms / 1000 51.1 ms；放大 50× → 500× |
| 单本台账写 | coverage 0.6–3.9 ms、todo 4.07 ms、note 4.01 ms |
| 水合 | 600 facts 7 ms、200 coverage 1.2 ms、100 todos 0.03 ms（**不是瓶颈**） |
| 派生产物全量重建 | 16.4–17.4 ms |
| 台账工具 token | coverage 1,502 / todo 1,146 / notes 1,143 = **3,791**；child 静态输入 13,130 |
| 机器推导数据源 | `run.json.tool_calls` 恒 0；`agents.db` 49–229 KB，解析 0.5–1.3 ms |
| 功能基线 | 52 passed / 0.74 s（5 个台账测试文件） |
| 权限缺陷 | 9/20 run 目录 `nobody:nogroup`，宿主不可读 |
| 一节点一文件读 | 1000 节点 **426 ms**（open 0.34 ms/个）；单文件 133 KB 2.9 ms → **退化 147×** |
| 单节点写 | tmp+rename+dir fsync **5.5 ms**；无 fsync 1.1 ms |

**测试边界**：未跑真实 LLM 扫描（无授权目标/密钥），token 是静态 schema 计量而非账单；并发锁争用与机器推导的语义准确率未测 —— 这正是 P2 保留 `update_coverage` 纠偏通道的原因。复核时请用 `usage.py` 的 cached_tokens 在真实 run 上对账。

---

## 九、相关工作

- `docs/ledger-optimization/evaluation-v1.md` — 减量合理性评估结论（批准 7→4；P0=WAL 非一节点一文件）
- `docs/ledger-optimization/p0-p1-impl-checklist.md` — P0/P1 并行实施清单（文件级改动与验收）
- `docs/ledger-audit-v1.md` — 26 本台账全量盘点、必要性三档分级、重叠矩阵
- `docs/sapper-ledger-comparison-v1.md` — 与 Sapper 的对照（它只有 1 本，靠"状态在结构里"）
- `docs/ledger-bench-v1.md` — 实测脚本、原始数据、对原方案的五点证伪
- `docs/prompt-slimming-audit.md` — 静态 token 基线（child 9,339）

*最后更新：2026-11-17 · 方案版本 v2*
