# Ben 项目台账盘点与优化建议（v1）

> 盘点日期：2026-11-17。方法：全码源梳理（`Ben/core`、`Ben/report`、`Ben/tools`、`Ben/interface`）+ 20 个真实 run 目录（`strix_runs/`）的落盘实测。
> "台账"定义：跨步骤持久化、用于记录事实/状态/进度/发现的存储机制（文件、JSON store、JSONL、SQLite、目录结构）。

## 一、结论速览

- **持久化台账 26 本**，另有 6 个纯内存"事实台账"（进度签名、MCP 注册表、recall 索引、共享发现索引、scan clock、mailbox——不落盘但承担调度真相）。
- 26 本中：**每 run 必写 8 本**（全自动）、**agent 工具驱动 7 本**（写了代码但其中 4 本真实 run 从未落盘）、**跨 run 产物 4 本**、**用户级 3 本**、**人工文档 4 处**。
- 三个核心发现：
  1. **覆盖台账空转**：可读的 11 份运行级 `coverage.json` 全部 `entries=0`（另 9 份因容器 UID 写入宿主无权读取）。即：设计上要求每个子 agent 调 `record_coverage`，实际 20 个 run 无一次有效记录。
  2. **"一条发现"有 4 个语义源、"一件待办"有 4 套并行系统**（详见 §四）。
  3. **人工维护成本其实很低**（绝大多数台账由代码/agent 自动写）；真正的成本在 **agent 的 token 消耗**（工具调用 + 提示词中常驻的台账使用指引）和 **26 套代码的维护负担**。
- 附带发现：9 个 run 目录的产物归 `nobody:nogroup` 且 `0600`，宿主侧（含 root）读不了——台账的**可读性没有保证**，这本身是缺陷。

## 二、台账清单（26 本持久化 + 6 个内存态）

### A 域：run 级产物（`strix_runs/<run>/`，随 run 生命周期）

| # | 台账 | 用途 | 写入方 | 使用频率（实测） | 维护成本 |
|---|---|---|---|---|---|
| L01 | `run.json` | 运行主记录：状态/预算/目标/最终结论 | 全自动，每次状态变化整文件原子重写 | 20/20 run | 低 |
| L02 | `vulnerabilities.json` | **发现台账（权威结构化源）**，ID `vuln-NNNN` | agent 工具调用 | 2/20 run（多数 run 无发现） | 中（agent token） |
| L03 | `vulnerabilities/<id>.md` | 发现单页人读版 | L02 自动派生 | 2/20 | 极低 |
| L04 | `vulnerabilities.csv` | 发现表格投影 | L02 自动派生 | 2/20 | 极低 |
| L05 | `findings.sarif` | CI 消费（SARIF 2.1.0，混入 coverage 结果） | L02/L06 自动派生，0 发现也写 | 20/20 | 极低 |
| L06 | `coverage.json`（运行级） | 覆盖台账对外版 | `.state/coverage.json` 自动派生 | 20/20 但 **entries 全 0（空转）** | 低写、**高提示词成本** |
| L07 | `penetration_test_report.md` | 执行报告（叙述性合并） | 自动派生 + finish 追加验证附录 | 5/20（多数 run 未走到 finish） | 极低 |
| L08 | `ben.log` | 运行日志（时序、含异常栈） | 自动（logging） | 19/20，最大 1.19 MB | 极低 |
| L09 | `heartbeat.jsonl` | 看门狗心跳 | 自动（supervisor，默认未启用） | **0/20（潜伏）** | 代码已写 |
| L10 | `orchestration_metrics.json` | 编排计数器 | 自动（埋点，每 +1 整文件重写） | **0/20（潜伏）** | 代码已写 |

### B 域：运行态镜像（`strix_runs/<run>/.state/`，resume 与多 agent 协作骨架）

| # | 台账 | 用途 | 写入方 | 使用频率（实测） | 维护成本 |
|---|---|---|---|---|---|
| L11 | `.state/coverage.json` | 覆盖台账运行时态（`record_coverage`/`update_coverage`） | agent 工具调用 | 2/20（空壳） | **高**：555 行工具代码 + 每个子 agent 提示词常驻指引 |
| L12 | `.state/notes.json` | 子 agent 笔记（6 类，含 `findings`） | agent 工具调用 | 3/20 | 中（501 行工具 + agent token） |
| L13 | `.state/todos.json` | 待办台账（7 个工具） | agent 工具调用 | 11/20 | **高**（624 行工具代码 + agent token） |
| L14 | `.state/threat_models.json` | 威胁模型（目标身份键控、增量 amend） | agent 工具调用 | 5/20 | 中（639 行） |
| L15 | `.state/task_graph.json` | fact/intent 图（新调度核心，untracked） | agent 工具 + 代码自动（seed/agent_finish 回灌） | **0/20（尚未启用）** | 已投入 705 行 |
| L16 | `.state/claims.json` | 认领/租约（`BEN_CLAIM=0` 默认旁路） | 代码自动（spawn 时） | **0/20（潜伏）** | 226 行 |
| L17 | `.state/verify_queue.json` | 验证队列（queued/upheld/refuted） | agent 工具 + validator 回写 | **0/20（潜伏）** | 394 行 |
| L18 | `.state/agents.json` | Agent 图快照（**resume 硬门槛之一**） | 自动（状态变更时） | 15/20 | 低 |
| L19 | `.state/agents.db` | Agent 会话 SQLite（**唯一可无损 resume 的载体**） | 自动（SDK 每轮落盘） | 15/20 | 低 |

### C 域：跨 run 产物（项目内）

| # | 台账 | 用途 | 写入方 | 使用频率 | 维护成本 |
|---|---|---|---|---|---|
| L20 | `recon/<host>_<ts>/` | 侦察记录（recon.json/md + raw/） | CLI 自动 | 6 次侦察 | 低 |
| L21 | `recon/<host>_<ts>_scout/` | 侦查 + **版本台账**（scout 四件产物） | CLI 自动 | 5 次（2 次写入中断只剩 raw/） | 低 |
| L22 | `reports/report_*.md` + `trace_*.json` | run 结束导出快照（归档逻辑未触发过） | 自动（`_persist()`） | 各 1 份 | 低 |
| L23 | `Ben/rag/vector_store/knowledge.sqlite3` | 知识库向量检索（629 chunks / 9.4 MB） | **人工** `python -m Ben.rag ingest` | 工具 `search_knowledge` 常驻 | **人工中高**（9.4 MB DB、需手工同步源料） |

### D 域：用户级（`~/.ben/`）

| # | 台账 | 用途 | 写入方 | 现状 |
|---|---|---|---|---|
| L24 | `cli-config.json` | CLI 配置持久化 | 自动 | 15 B |
| L25 | `keypool.json` | 模型密钥池（0600） | 人工/CLI | 434 B |
| L26 | `mcp-servers.json` | MCP 服务器登记 | 人工 | **文件不存在（未配置）** |

### F 域：人工维护（对账人员真正用手维护的部分）

| 台账 | 位置 | 现状 |
|---|---|---|
| 设计文档 | `docs/*.md` 8 篇 196 KB | 人工，活跃 |
| 修复验证清单 | `docs/fixes/README.md` | 人工，是 4 本潜伏台账的唯一文档 |
| 评审快照 | `reports/2026-11-*.md` 3 篇 | 人工，**与机器导出物混放同一目录** |
| 根 `skills/` | 8 个 SKILL.md / 100 KB | 人工维护但**代码不加载**（真加载的是 `Ben/skills/`）——纯死重 |

### E 域：内存态（不落盘，但一旦格式变动即受牵连）

进度签名（`progress.py`，聚合 L02/L11/L13/L16/L18）、MCP 注册表（状态投影进 `run.json`）、recall 索引（从 L11/L12 重建）、共享发现索引（`prompt_budget.py`，合并 L02+L12）、scan clock、mailbox（快照进 L18）。

## 三、必要性评估

**A 级（必需，动不得）**：L01 `run.json`、L18 `agents.json`、L19 `agents.db`（resume 链）、L02 `vulnerabilities.json`（发现权威源）、L14 `threat_models.json`（威胁建模方法论核心，被 root/子 agent 提示词多处引用）。

**B 级（自动派生的投影，零人工成本，保留但可降频）**：L03/L04/L05/L07——全是 L02/L06 的渲染物。注意两点：0 发现也全量写 SARIF（为覆盖旧文件，合理但可注明）；`_write_artifacts()` 每次状态变化重渲染全套，可改为 finish 时一次性生成。

**C 级（空转，需"修好或砍掉"）**：L06/L11 覆盖台账。20 个 run 无一条有效记录，但每个子 agent 的系统提示词里常驻着"覆盖台账工具配合要点"（有专门测试 `test_coverage_reconciliation_prompt.py` 锁定这段文案）——**纯支出零产出**。这是全项目必要性最可疑的一本。

**D 级（潜伏，需 enable-or-delete 决策）**：L09/L10/L15/L16/L17 共约 2,200 行代码、0 个 run 使用过。其中 L15 task_graph 是**已在推进的新方向**（untracked 新代码、`docs/dfs-on-demand-design-v1.md` 有完整设计），保留；L09/L10/L16/L17 是配套观测点，等待与 L15 一起启用；`docs/fixes/README.md` 已把它们定义为验证口子。

**E 级（冗余/语义重叠，可合并）**：
- L12 `notes[category=findings]` 与 L02——同一"发现"两种写法；
- L13 todos 与 L15 intents——双待办系统；
- L16 claims 与 L15 `intents[status=claimed]`——"谁在做什么"两套表达（同一 intent 的锁）；
- L17 verify_queue——本质是 L02 的 `verification_status` 外挂表；
- 根 `skills/` 与 L23 RAG——两套知识供给互不相通，且前者是死代码路径。

**F 级（人工台账，问题在组织不在数量）**：`reports/` 里人工评审快照与机器导出物混放；`CLAUDE.md` 的台账清单只覆盖 8/26；`notes/tools.py` docstring 还写着 "live in-memory only"（实际已落盘）。

## 四、重复与合并机会（重叠矩阵）

```
一条"发现"：  L02 权威源 ─(自动派生)─> L03 md / L04 csv / L05 sarif / L07 报告   ← 派生链没问题
              L12 notes[findings]（agent 自由文本）      ← 语义重复
              L15 facts[kind=vuln]（图节点）             ← 设计中，已有唯一合并点：
                                                          prompt_budget 只并 L02+L12，不含 L15
              agent_finish 的 result_json（退出契约）     ← 已自动回灌 L15，问题不大

一件"待办"：  L13 todos（agent 自述）
              L15 intents（调度器派生）                    ← 双待办，边界无文档
              L11 coverage 行（surface×risk）              ← 空转
              L16 claims（同键租约）                       ← 潜伏

"谁在做什么"：L16 claims ⇄ L15 intents[claimed] ⇄ L18 agents.statuses   ← 三投影

"目标是谁"：  L14 _target_identity / L20 recon 目录名 / L01 targets / L15 facts[host]
              —— 三处独立的 target 归一化实现

去重机制五套：L15 sha1 身份键、L11 surface×risk、L12 标题去重、L16 claim 键、
              report/dedupe.py 的 LLM 语义判重
```

设计文档 `docs/dfs-on-demand-design-v1.md §4.3` 明确写了 "fact dedupe ≠ coverage ledger，不要合并"——重叠是**已知且被文档主动约束**的设计债，不是疏忽。但现在 L15 落地（untracked、0 run 启用）恰好是收敛这套债的窗口期。

## 五、优化建议（按优先级）

### P1（收益最大）：覆盖台账改为"机器自动生成 + agent 只补差额"

现状是每个子 agent 被要求手填 coverage，实际从不填。方案：
- `build_coverage_document()` 已有 `machine_observed` 概念——把它从"补充"升为"主体"：从 `agents.json`（agent 图）+ `task_graph` facts/intents + `run.json` 的 tool_calls **自动推导** entries（谁探测了哪个 host/service/dir/endpoint、结论如何）；
- agent 侧只保留 `update_coverage` 用于**修正**机器判断（如把 `no_issue_found` 改 `needs_follow_up`），`record_coverage` 的"从零登记"职责取消；
- 提示词中的"覆盖台账工具配合要点"相应缩减为 1–2 句。
- 收益：空转台账变为真实产出；每个子 agent 的提示词与工具调用预算下降；`test_coverage_reconciliation_prompt.py` 同步更新。

### P2：待办三轨收敛为 task_graph 单轨

- 以 L15（新调度核心）为唯一"待办"事实源；
- L13 todos 工具降级为**只读视图**（渲染 intents）或直接退役——当前 11/20 run 有 todos 说明 agent 确有用它的惯性，可先保留 create/list 但内部写入 task_graph intent；
- L16 claims 整本删除，claim 状态内联为 `intents[status=claimed]`（其 TTL/防抢逻辑迁入 task_graph）；
- `.state/coverage.json` 的 `surface×risk` 键与 intent 键统一为 task_graph 的 `dedupe_key`。
- 收益：三套并行状态机 → 一套；`progress.compute_world_signature` 的输入源从 5 本台账降到 3 本，漂移风险减半。

### P3：发现单源化

- `notes` 的合法 category 去掉 `findings`（或改为写入时自动投影成 L02 的草稿条目，状态 `draft`）；
- `verify_queue` 内联为 L02 上的字段（`verification_status` 已存在，队列只是它的外挂），finish 时直接消费，删掉 `.state/verify_queue.json`；
- 保留 `prompt_budget` 的 L02+L12 合并索引（这是唯一已工作的合并点）。

### P4：潜伏台账 enable-or-delete

- L15（task_graph）：按计划启用，其余 L09/L10/L16/L17 随它一起打开或一起删；
- 半年内仍为 0 使用且不随 L15 走的（`heartbeat`/`orchestration_metrics` 若最终不启用 supervisor），连同 `docs/fixes/README.md` 的验证口子一起删——**留代码不留台账**比留一套 0 频率的机制便宜。

### P5：派生产物降频 + 知识双轨合一

- `_write_artifacts()` 改为"状态变化只更新 L01/L02 源文件；L03/L04/L05/L07 在 finish 或 view 时按需渲染"——每 run 少几十次全量重写；
- 根 `skills/`（8 篇、代码不加载）：迁入 `Ben/rag/knowledge_base/` 走 ingest，或删除；`Ben/skills/` 与 `load_skill` 保持不变；
- RAG 源料→DB 同步加一个 `make rag-ingest` 快捷命令，降低 L23 的人工维护摩擦。

### P6：文档与目录卫生（低成本高回报）

- `CLAUDE.md` 的台账清单从 8/26 补齐到含 `.state/` 七件套（一行一个即可）；
- 修 `notes/tools.py` 过期 docstring；
- 人工评审快照移出 `reports/`（如 `docs/reviews/`），机器导出物独占 `reports/`，归档逻辑触发时不再混淆；
- 修容器 UID 写入问题（9/20 run 宿主不可读）：run 结束时 `chown` 回当前用户，或以固定 UID 跑容器——否则台账连"事后可审计"都做不到。

### 建议不动的地方

- L01/L18/L19 resume 链、L14 威胁模型、SARIF 双内容设计（发现 + 覆盖塞同一 SARIF 是刻意的 CI 集成方案）；
- `dfs-on-demand-design-v1.md` 的"fact 与 coverage 不合并"原则在 P1 落地前仍应遵守。

## 六、预期收益量化（粗估）

| 维度 | 现状 | 优化后 |
|---|---|---|
| 每子 agent 常驻提示词 | coverage 指引 + todo 7 工具 + notes 5 工具 schema | coverage 指引缩至 1–2 句，todo/notes 工具减半 |
| 状态源数量 | 1 条"待办" 4 套、1 条"发现" 4 源 | 待办 1 套（task_graph），发现 2 源（L02 权威 + L12 草稿） |
| 台账代码量 | 26 本 ≈ 6,000+ 行相关模块 | 预计 -800～1,200 行（claims/verify_queue/coverage 双份） |
| 人工维护 | docs + RAG ingest + 根 skills（死重 100 KB） | 删死重，RAG 一键化 |
| run 产物可读性 | 9/20 不可读 | 修复 chown 后 100% |

---

## 七、口径确认（v1.1，2026-11-17 复核）

> 本节由二次评审确认，与 §一~§六 的差异以此节为准：原文"26 本"把两类东西混在一个数字里，现拆为两层口径。

**两层口径**：
- **业务台账（约 7 本，`.state/` + 发现权威源）**：回答「发现了什么 / 该做什么 / 测出什么 / 谁在做」——vulnerabilities、task_graph、coverage、todos、claims、verify_queue、notes、threat_models。
- **产物与投影（其余大半）**：strix_runs/ 导出、日志、配置、`~/.ben/`——自动派生或基础设施，**不是协作语义**，不参与"合并语义"讨论。

**收敛后的目标形态**：权威发现 = `vulnerabilities.json`；期望/差集 = baseline（未来，且必须后置）；发现+调度 = `task_graph`（fact/intent，含内联 claim 状态）；结论纠偏 = coverage（机器推导 + 少量 update）；方法 = threat_models；记忆 = notes（非 findings）；会话 = agents.json / agents.db / run.json。

**执行顺序**（取代 §五 的优先级表述）：
P1 coverage 机器化 → P2 待办单轨（intent 为主、claims 内联、todos 降级）→ P3 发现单源（notes 禁 findings、verify 内联）→ P4 投影降频 + UID 可读性 + 死 skills/RAG 卫生 → **其后**才上 baseline/reconcile（防漏扫），挂在修好的 task_graph + 机器 coverage 上。

**明确不做**：
1. 不把 fact 与 coverage 合成一本（设计已约束：身份去重 ≠ 面结论）；
2. 空转未修前不加 scan_ledger / 独立 retry_queue 文件（attempts 先长在 intent 上）；
3. 不动 resume 链与 vuln 权威源。

## 八、coverage 机器化的边界条件（v1.1 补充）

机器推导的 entries **默认不得自动落 `no_issue_found`**（"跑过命令" ≠ "对面有结论"）。机器条目的合法初始 outcome 应是 `needs_follow_up` 或未定，由 `update_coverage` 纠偏后才升级——否则机器化只是把"agent 空转"换成"机器制造假覆盖"，比空转更糟（假完整性会误导 frontier 排除集与 SARIF）。溯源字段（agent_reported / machine_observed）必须随条目保留。

---

*本报告基于 2026-11-17 的代码与 20 个 run 目录实测；task_graph 相关代码为 untracked 新代码，落地节奏可能改变 P2 的优先级。*
