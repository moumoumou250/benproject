# 台账为什么这么多：与 Sapper 的对照分析（v1）

> 对照对象：`/home/kali/Desktop/AI/sapper-v2`（lib 5,611 行，纯 stdlib；Sapper 为第三届腾讯云智能攻防挑战赛线上赛冠军，8 小时无人值守、10 亿 token 打完 85 flag）。
> 我方基线：`docs/ledger-audit-v1.md`（26 本持久化台账，其中业务语义 7 本）。

## 一、结论先行

| | Sapper | Ben |
|---|---|---|
| 业务台账 | **1 本**（`.sapper/` 一张图：facts × intents × hints） | **7 本**（vulns / task_graph / coverage / todos / claims / verify_queue / notes + threat_models） |
| 持久化产物总数 | 3–4 件（图 / 交接 md / KB / 日志） | 26 本 |
| 看板 | `render_board()` 读时渲染，**不落盘** | coverage.json、vuln md/csv、trace.json **均落盘** |
| 状态表达 | 图结构（`from`→`to` 证据链），**无状态机** | 每本台账自带 status 枚举 + 状态机 |
| 恢复方式 | 个体死了，继任者读图接岗 | 会话回放（`agents.db` 逐项 restore） |
| 知识 | 56 条 JSON，token 重叠打分，无 embedding | 9.4 MB SQLite 向量库 / 629 chunks |
| 代码量 | 5,611 行（含 arena 平台适配、sweep、hook） | 仅台账相关模块 ≈ 6,000+ 行 |

**一句话**：差距不是"需求差 26 倍"，而是三条设计选择不同——**图 vs 状态机、算出来的视图 vs 存下来的视图、重启换人 vs 恢复会话**。

## 二、为什么我们会维护这么多台账

台账数量大致 ≈ **读者种类 × 写入时机种类**。Sapper 把两者都压到 1（读者只有"读图的 agent + 指挥官"，写入只有"CLI 被调用时"）；我们把它们各自展开了。具体有五个成因：

**1. 事故驱动增量（最主要）**
每遇到一次真实故障就加一本，而不是加一个字段：
- `claims`：并发抢单重复派活 → 加租约台账；
- `verify_queue`：假阳性进了报告 → 加验证队列；
- `coverage`：怕漏扫 → 加覆盖台账；
- `todos`：agent 需要拆解 → 加待办台账。
五本台账 = 五次事故的化石。Sapper 面对同类问题（谁在做、做完没、做对没）的解法是：**改 intent 的字段**（`worker`/`status`/`to`），不加文件。

**2. 进程内多 agent 的恢复需求**
我们是同进程 asyncio 多 agent，崩溃后要精确恢复，于是有 `agents.json` 快照、`agents.db` 会话库、`progress` 签名、`claims` TTL。Sapper 是多进程 tmux 窗格，它的恢复哲学是"**个体可以消亡，文明必须繁衍**"——士兵上下文满了就换人，新兵读图接岗，不做会话回放。这一条决定了我们必然比它多 2–3 本。

**3. 产品化契约**
run.json（web 控制台历史库）、SARIF（CI Code Scanning）、执行报告（人读）、usage（计费/预算）——Sapper 是竞赛 harness，分数由平台记，没有 CI、没有控制台、没有账单，这四本它一本都不需要。

**4. 把"视图"当"数据"存**
这是最没有道理的一类。coverage.json 是"从 agent 图和工具调用推出来的视图"，vuln md/csv/trace 是"从 vulnerabilities.json 渲染的视图"，但我们全都持久化了，于是视图也有了生命周期、字段、schema、版本迁移。Sapper 的看板证明了另一种可能：**board 每次现算，<100 行文本，零存储**。

**5. 缺少统一图作为收敛点**
Sapper 从第一行代码就有 fg（fact graph）基座，所有能力都长在同一张图上；我们是 strix 的本地化分叉，每个子系统在不同时间点自建 schema，结果长出 **5 套去重机制**（sha1 身份键 / surface×risk / 标题去重 / claim 键 / LLM 语义判重）。`task_graph` 是后来补的收敛点，但老台账还在并行跑。

**核心判断**：数量本身不是问题，**"同一事实有多个权威"才是问题**。26 本里 19 本是投影与基础设施，真正需要收敛的是那 7 本业务台账。

## 三、Sapper 的台账清单（实测）

```
.sapper/                        ← 每个 challenge 一个，多 agent 用符号链接共享
├── project.json                目标 + 计数器（唯一"元数据"文件）
├── facts/   f001.json …        host / service / cred / vuln / loot / flag / note
├── intents/ i001.json …        from(依赖facts) → to(产出的fact) + status + worker
├── hints/   h001.json …        指挥官→工人的情报
├── ai-peers.json               通信对端白名单
└── .lock                       fcntl.flock，多进程并发安全
commander-state.md              <150 行交接文档（唯一人/agent 手写的台账）
kb/entries/kb001…056.json       56 条知识，token 重叠打分
drive.log / pane-env/*.env      基础设施日志（不是台账）
```

四条设计规则（来自 `sapper.py` 头注释与 SKILL.md）：

1. **"State is encoded in graph structure, not in a state machine."**
   intent 是否 ready = `status==open` + `from` 的事实全部存在 + 未待确认。因为 facts 不可变，"存在"就等于"依赖满足"。没有额外状态字段，更没有状态机台账。
2. **一节点一文件 + flock**：可 git diff、并发安全、崩溃最多损坏一个节点（还有 schema 校验 + 跳过坏文件）。
3. **不可变约定 + 单一可变例外**：facts 写入即不可变；唯一例外是 flag 的 `data.status`（recorded→submitted/rejected），因为提交结果是外部世界的回执。
4. **claim 不是台账，是 intent 的字段**：`status=claimed` + `worker`。我们用了 226 行 `task_claims.py` + 一个 `claims.json` 做同一件事。

**Sapper 没有的东西**（对照我们）：
- 没有 coverage ledger（覆盖 = 未完成的 open intents，天然隐含）
- 没有 todos（待办 = intents）
- 没有 notes 库（笔记 = `kind=note` 的 fact）
- 没有 verify 队列（验证 = `dispatch verify` 产出一条新 fact）
- 没有 run.json（project.json 只有 goal + counters）
- 没有 SARIF/CSV（比赛不需要 CI 集成）
- 没有向量库（56 条 JSON + 打分）

## 四、同与异

**相同的底层判断**（说明我们的方向没错）：
- 都以"**事实**"为最小单位，而不是以"消息/日志"为单位；
- 都不信 agent 的自述：Sapper 用 `from→to` 证据链，我们用 `verification_status` + 机器观测；
- 都追求可恢复：它靠图，我们靠快照；
- 都强调单一权威：它明确"两个项目两张图，永不互见"；我们也在往 task_graph 收。

**关键差异**：

| 维度 | Sapper | Ben | 谁更优 |
|---|---|---|---|
| 状态载体 | 图结构（关系即状态） | 各台账的 status 字段 + 状态机 | Sapper 少一半状态迁移 bug |
| 存储粒度 | 一节点一文件 | 少文件整体重写（run.json 每次全量） | Sapper 并发/崩溃更稳 |
| 视图 | 读时渲染 | 写时持久化 | Sapper 少一半 schema |
| 并发 | 多进程 + flock | 单进程 asyncio + 内存锁 | 各自匹配架构 |
| 恢复 | 换人读图 | 会话逐项回放 | Ben 无损，Sapper 便宜 |
| 覆盖完整性 | 平台记分（外部真值） | 自证（coverage） | 场景不同，不可直接比 |
| 知识 | 56 条 JSON + 打分 | 629 chunks 向量库 | 我们的规模下 Sapper 更划算 |
| 人工台账 | 1 份交接文档 | docs 8 篇 + 评审 3 篇 + 死 skills 8 篇 | Sapper 更克制 |

## 五、为什么不能简单照搬

1. **resume 是产品契约**。`agents.db` 是唯一可无损恢复的载体，Sapper 的"换人"策略在竞赛里成立（重跑一遍很便宜），在产品里不成立（一次扫描可能烧几十美元）。
2. **SARIF / 执行报告是交付物**，必须落盘，不能改成即时渲染。
3. **进程模型不同**。它是 tmux 多进程，我们是 asyncio 单进程；它的 flock 方案我们不能照抄（但可以让 task_graph 用 per-node 文件 + 现有 `jsonstore` 原子写）。
4. **它没有"覆盖自证"需求**。CTF 有平台记分当外部真值，我们的客户没有——coverage 必须存在，但**应该由机器从图上推导**，而不是让 agent 手填（这正是 ledger-audit 的 P1）。
5. **规模不同**。它单机单用户 56 条知识；我们有 20 个历史 run、Web 控制台、TUI、CI。

## 六、可以借鉴的 7 条（按收益排序）

**B1. 把"状态在结构里"用到 task_graph（收益最大，直接支撑 P1）**
- intent 上增加 `attempts`、`verification`、`outcome` 三个字段，替代 `verify_queue.json` 与 `claims.json`；
- **coverage 改为派生视图**：`open/abandoned intents` + 未覆盖 surface = gaps，不再单独落盘 `.state/coverage.json`；
- 这一条同时满足我们已确认的边界：机器条目默认只能是 `needs_follow_up`，不会制造假覆盖——因为"没有对应 fact/intent"本身就是"没结论"。

**B2. task_graph 改为一节点一文件 + flock**
现在 705 行代码对着一个大 JSON 整写，并发靠进程内锁。改成 `facts/f001.json`、`intents/i001.json` 后：崩溃只损一个节点、可 git diff、多个 agent（未来若跨进程）天然安全。成本中等，但这是把 task_graph 变成"唯一权威"的前提。

**B3. 加一个 `ben board` 单一读口，替代持久化投影**
`render_board()` 的形态值得照搬：从 run.json + task_graph + agent 图现算一张 <100 行表。有了它，`coverage.json`、`vulnerabilities.csv`、`trace.json` 都可以改为按需生成（SARIF 与执行报告除外，它们是交付物）。

**B4. 不可变 + 单一可变例外**
`vulnerabilities.json` 现在是可变的（update_history、deleted 列表、ID 不回收）。Sapper 的 flag 台账给了更好的形态：**新增即新节点，状态变更只改一个 `status` 字段**，"更新历史"由追加的 fact 自然表达，不必在报告里维护一份 update_history 全文。

**B5. claim 内联（现成范本，直接落地 P2）**
`intent.worker` + `status=claimed` + `claimed_at/ttl`。删 `claims.json`，`DEFAULT_CLAIM_TTL_MINUTES=67.5` 的逻辑迁进 task_graph 的 claim 函数。Sapper 已证明这个形态能撑 8 个窗格共享一张图。

**B6. KB 走"JSON 索引 + 两段式"快路径**
56 条 JSON、标题权重 ×2、token 重叠打分、`show` 才给全文。我们的 629 chunks / 9.4 MB 对 38 个源文件是过度工程，而且 ingest 是纯人工。建议：保留向量检索作为可选后端，但增加"JSON 索引"快路径（`title + description + tags` 打分），让日常答疑不再依赖人工 ingest。

**B7. 长跑加一份 <150 行交接文档**
resume 时先读 digest，再决定是否回放 session。省 token，也让"人接手"变可能。Sapper 的 `commander-state.md` 明确写了 "<150 lines"，这个硬上限值得抄。

## 七、明确不借鉴的

- **不砍 agents.db / run.json**：resume 与审计凭据，产品契约。
- **不把 SARIF / 执行报告改成即时渲染**：交付物必须落盘。
- **不引入 tmux 多进程模型**：架构改动面过大，收益不匹配。
- **不照抄"没有 coverage"**：CTF 的平台记分是外部真值，我们没有；但要照抄"覆盖是图上的派生物，不是一本手填的账"。
- **不为了"像 Sapper"而重写**：Sapper 是冠军验证过的**形态**，不是可以直接搬的**代码**（它连 import 都只有 stdlib，我们依赖 pydantic / LiteLLM / openai-agents）。

## 八、结论

Sapper 用"**一张不可变的图 + 读时渲染的视图 + 换人读图**"把协作台账压到 1 本。我们的 26 本里，19 本是投影和基础设施（其中一部分完全可以现算），真正该收敛的 7 本业务台账，正好对应 `ledger-audit-v1` 的 P1–P3：

- P1 coverage 机器化 ≈ Sapper 的"覆盖是图的派生物"；
- P2 待办单轨（claims 内联、todos 降级）≈ Sapper 的"intent 字段即状态"；
- P3 发现单源（notes 禁 findings、verify 内联）≈ Sapper 的"flag 只是 fact"。

Sapper 的价值不在它"台账少"，而在于它**从第一天就只有一个写入点**。我们做不到"从第一天"，但可以在 task_graph 上把"第二天"做成第一天。

---

*依据：sapper.py 头注释与 Store 实现、SKILL.md 核心模型、SPEC.md 运行时契约、kb.py；我方数据来自 `docs/ledger-audit-v1.md` 及 20 个 run 的落盘实测。*
