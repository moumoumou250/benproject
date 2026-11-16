# Sapper-v2 编排机制深读 & Ben 编排层优化方案

> 范围：只讨论**编排**（谁决定做什么、怎么派、什么时候停、失败怎么救），不重复此前 `sapper-vs-ben-optimization-report.md` 的 token / 代码结构议题。
> 方法：逐文件读实现（行号见文末证据索引），所有"效果"数字均为**估算**，标注了估算依据与验证方式。

---

## 0. 结论摘要

Sapper 的编排不是一个"更强的 agent"，而是把**调度决策从模型脑子里搬到磁盘上**：

| 层 | 机制 | 解决的问题 |
|---|---|---|
| 状态层 | typed fact graph（fact/intent 节点 + DAG 边 + 原子事务 + flock） | 多个 agent 共享同一份事实，不靠对话同步 |
| 调度层 | frontier → pick → **claim** → done / verify；`auto` 的图签名停机；`sweep` 轮转 | 谁做哪件事有唯一归属，空转有硬停机 |
| 执行层 | tmux pane = 进程隔离；`worker.sh` = 指挥官唯一的"手" | 上下文天然重置，指挥官不被 worker 的输出淹没 |
| 守护层 | watchdog：`--continue` 重启、key 轮换、到点 buzzer | 长跑不靠模型自觉 |

Ben 的编排骨架其实更"现代"（单进程 asyncio + AgentCoordinator + 结构化 coverage/vuln 账本 + 预算 pause/resume），**缺的恰好是 Sapper 那层"调度纪律"**：没有认领、没有就绪队列、没有无进展停机、没有 run 级时钟、没有角色档位。

八项差距里，**G1（无认领）、G3（无空转停机）、G5（无角色档位）** 是投入产出比最高的三项，且都不需要改架构，只加薄薄一层。按本方案 P0 三项落地，估算可把单次扫测的**重复劳动降 10~20%、空转轮次降 30~50%、子 agent 调用成本降 15~30%**（依据见 §5）。

---

## 1. Sapper 编排架构：三个控制面 + 一个数据面

```
                    ┌──────────────────────────────────────────┐
   控制面 1          │ commander (smart TUI, SKILL.md 426 行教条) │
   战略/配额/收口     │  每 60s: board → 思考 a0~e → 最多 2 个动作  │
                    └───────────────┬──────────────────────────┘
                                    │ worker.sh spawn/msg/peek/kill/board
                    ┌───────────────▼──────────────────────────┐
   控制面 2          │ worker.sh（776 行 bash，一次性命令，幂等）  │
   派发/隔离         │  spawn 时按难度选 model / minutes / 形态    │
                    └───────────────┬──────────────────────────┘
                                    │ tmux new-window
                    ┌───────────────▼──────────────────────────┐
   执行面            │ pane: claude TUI  │ pane: sapper auto -p  │
                    │ （深挖，会话持久）  │ （定时杂活，到点自停）  │
                    └───────────────┬──────────────────────────┘
                                    │ 读写同一份 .sapper/
                    ┌───────────────▼──────────────────────────┐
   数据面（唯一真相）  │ facts/*.json + intents/*.json + hints    │
                    │ flock 串行化，atomic_write_json 落盘       │
                    └───────────────┬──────────────────────────┘
                                    │ 读图/重启/轮换
                    ┌───────────────▼──────────────────────────┐
   控制面 3          │ watchdog（658 行）：--continue 重启、       │
   自愈/兜底         │ key 轮换、stall 不干预、到点强制收尾        │
                    └──────────────────────────────────────────┘
```

### 1.1 状态层：图即接口

- **节点只有两类**：`fact`（观察到的世界）与 `intent`（待做的工作）。intent 用 `from: [fact_id...]` 指向依赖事实，`to: fact_id` 指向产出——**这就是 DAG 边**，不需要单独的依赖表（`lib/sapper.py:833 is_ready`：`intent["from"] ⊆ fact_ids` 才算 ready）。
- **三层记忆控制注入量**：每个节点有 `title / description / doc`。图渲染只出前两层（`render_graph_text`，`sapper.py:1151`），`doc` 只在 `sapper doc <id>` 时读。这是"上下文按需展开"的物理实现。
- **并发正确性靠文件锁而非进程内锁**：`Store.locked()` 是 `fcntl.flock(LOCK_EX)`（`sapper.py:181-188`），`claim_intent` / `conclude_with_new_fact` 都在锁内做"读-判-写"。后者注释写得很直白：**若 intent 已被并发 executor 结束，会在写任何 fact 之前抛错，不产生孤儿事实**（`sapper.py:479-520`）。

### 1.2 调度层：三套调度器，按"时间尺度"分工

| 调度器 | 代码 | 时间尺度 | 用途 |
|---|---|---|---|
| `frontier/pick/claim/done` | `sapper.py:938 / 1639 / 2821 / 2838` | 秒级 | 单挑战内的任务分派；claim 保证唯一归属 |
| `auto`（reason→explore→verify） | `sapper.py:3727-3874` | 分钟级 | 无人值守推进一张图，`-p` 杂活/小队用它 |
| `sweep` | `sapper.py:2647-2723` | 小时级 | 多挑战轮转，每轮给每个未完成挑战 `--minutes-per` |

**`auto` 的五个停机条件是精华**（`sapper.py:3760-3870`）：

1. `deadline` 到（`--minutes`/`--deadline`，每步开头判）；
2. 图状态 `completed`；
3. reason 连续两次 `noop/rejected`；
4. reason 连续两次异常（`reason_failures >= 2`，防止 API 抖动打死循环）；
5. **图签名不变**：每步前取 `_graph_signature()`（facts 的 id+description、intents 的 id/to/confirmed_at），工作做完后再比一次，相等即 "no graph change after work — stopping"。

第 5 条是 Sapper 最便宜的空转保险：不判断"模型有没有努力"，只判断"世界有没有变"。

`auto` 还有两个配额：`--max-steps`（reason 轮数）与 `--max-explore`（本次最多展开几个 intent），避免一个 run 把图刷爆。

### 1.3 执行层：pane 是上下文隔离的单位

- 每个 worker 是一个独立进程（`tmux new-window`），**上下文天然不共享**；共享的只有磁盘上的图。
- 因此"给 worker 讲清楚背景"变成显式动作：`--mission "<文本>"`，模板化写在 `worker.sh:242 general_mission` / `:247 default_mission`，里面固定包含：目标地址、旗标提交命令、记录规范（`sapper fact ... -k service -d host=...`）、HELP/REPORT 协议、DONE 语义。
- 通信不是消息队列，而是**往图里写 note**：worker 写 `[HELP]` 事实，指挥官每轮 `board` 时看到 helps 计数，再 `msg` 回去（`SKILL.md:49-54`）。消息可能丢（pane 死了），但图不会。

### 1.4 守护层：恢复靠 cwd 作用域，而非会话 id

- `claude_resume_flag()` 检查 `~/.claude/projects/*<basename(cwd)>*` 是否存在，有才加 `--continue`（`watchdog.sh:289-293`）。同一挑战的 w1/w2 用**不同 cwd**（`<code>` 与 `<code>-w2`，图目录是符号链接），所以 `--continue` 不会串会话——这是"多 agent 同图不同脑"的关键细节。
- worker 退出 → 每个 pane 最多重启 3 次（`watchdog.sh:636`）；quota/auth 错误 → 轮换 key 并同步 tmux session 环境（`watchdog.sh:157-172`）；**stall 故意不干预**（`watchdog.sh:650-651`，注释：nudging 是指挥官的职责）。

### 1.5 决策纪律：commander 的教条才是"编排算法"

`SKILL.md` 426 行里，真正起调度作用的是四块：

1. **循环固定七步**（`:41-67`）：读 STATE → `board` → 按 a0(HELP)/a(未提交旗标)/b(15 分钟无新事实)/c(可迁移情报)/d(完成的实例)/e(空出的实例槽) 顺序思考 → **最多两个动作** → `sleep 60`。
2. **升级表是唯一真相**（`:178-195`）：0-10min 发战术指令（免费）→ 10-20min 开第二角度 → 20-30min 上 general（唯一默认 smart 花费）→ 30-45min park。配套**配额账本**：smart 档只允许四类岗位（升级表 20-30 行、硬判定派单、半场审计、残局重开）。
3. **85/90 压缩协议**（`:264-279`）：上下文 ~85% 或 `ITERATIONS` 到 8 时，停手重写 STATE（分配表/停放清单/情报日志/队列），90% 交给 harness 压缩。**STATE 文件是跨压缩、跨重启、跨死亡的唯一连续性**，并规定"任何 fresh start 第一动作 = 读 STATE"。
4. **半场与残局**（`:322-338`）：`budget_left` 首次低于一半时检查 `generals=` 占比，低于 20% 就是"囤积 smart 火力"，强制派 general；最后 1/4 预算重开 parked，剩 20 分钟**停止一切 spawn**，只做提交与收口。

### 1.6 关键数字

| 指标 | 值 | 出处 |
|---|---|---|
| 内核单文件 | `lib/sapper.py` 4,726 行，纯 stdlib | `wc -l` |
| 模板总量 | 22,310 字节（explore 7.7K / reason 5.7K / verify 1.8K） | `wc -c templates/*` |
| 并发上限 | 8 pane / 3 挑战实例 / smart 并发 ~3 | `SPEC.md:26`、`SKILL.md:212-214` |
| 总时钟 | `SAPPER_TOTAL_MINUTES=470`，每轮把 `time_budget` 注入 prompt | `SPEC.md:23`、`sapper.py:3764` |
| 指挥周期 | 60s 一轮，每轮 ≤2 个动作 | `SKILL.md:62-65` |
| 单挑战预算 | ≈23 分钟（24 slot-hours / 63 challenges） | `SKILL.md:179` |

---

## 2. 一次典型循环的时序（把上面的零件串起来）

```
T+0     commander: cat STATE（仅冷启动）→ worker.sh board
        board = arena 平台行 + 各 code 的 facts/kinds/intents/flags/helps + pane 空闲时长
T+1s    判 a0: e07 有 [HELP] → msg e07-w1 "试 /api/v2 的 JWT kid 注入，先读图里 f12"
T+2s    判 e: 空出 1 个实例槽 → 按 4.6 第一行，给最易未开的 e12 派 4 分钟 scout
        worker.sh spawn e12 --p --minutes 4 --mission "Scout only: ..."
T+60s   sleep 结束，回到 board
T+5m    e12 pane 空闲 → peek 120 → 读 facts → kill → 判定 easy → spawn e12（flash 士兵）
T+18m   e12 15 分钟无新事实 → 升级表 10-20 行：开 w2 不同角度（source audit）
T+40m   仍无进展 → 上 general（smart，~25 分钟）
T+65m   general 卡住 → park：kill pane、kill --close、STATE 写原因
T+...   auto 内部：每步 reason→explore，图签名不变即停；verify 失败写 hint 回图
T+end   剩 20 分钟：停止 spawn，扫 recorded 旗标补提交，watchdog buzzer 兜底
```

对照 Ben：这条时序里的**每一个"判"都是一个可编程规则**，而 Ben 目前把所有"判"都留给了 root 的 12 个决策轮。

---

## 3. Ben 现有编排骨架（先说公平的部分）

```
CLI → AgentRunner.run_async
      ├─ 预算/暂停：ReportUsageHooks（70/85/95 三档告警 + stop/pause 两种策略）
      ├─ 每轮 LLM 前：_maybe_inject_frontier（≤6KB，签名不变不重复注入）
      └─ run_agent_loop（SDK Runner 多轮）
            ├─ root（LLM_MODEL，挂 finish_scan，max_turns=12）
            └─ create_agent → spawn_child_agent（BEN_FLASH_MODEL，in-flight≤4，默认 45min 时限）
                  ├─ 共享账本：coverage（surface+risk_area，可 update）、notes、vulnerabilities.json
                  └─ 通信：mailbox（send_message / wait_for_agents / agent_finish 报告进父收件箱）
```

尺寸：`Ben/core` 合计 5,190 行（runner 1,547 / execution 1,423 / agents 709 / hooks 487 / frontier 278）。

**已经对齐甚至超过 Sapper 的部分**（不要重复造）：

| 能力 | Ben 实现 | 对比 Sapper |
|---|---|---|
| 上下文压缩 | SDK compaction + cache_point | Sapper 靠"节点三层 + 新进程"，无压缩 |
| 递归子 agent | 子可再派生（in-flight 计数含孙） | Sapper general 可雇 squad，但只有一层 |
| 预算策略 | stop / pause 双策略，可在线加预算 | Sapper 只有总时钟，无美元预算 |
| 证据绑定 | `create_vulnerability_report` 强制 HTTP exchange id 对得上 Caido（`reporting/tool.py:231`） | Sapper verify 只是模型判断 |
| 续跑 | `-r` + resume 注入"已排除面"（`frontier.py:45-91`） | Sapper 靠 STATE 文档人肉交接 |
| 子 agent 时限 | `BEN_SUBAGENT_MINUTES` 45min + `create_agent(minutes=)` | Sapper `--minutes` 更细（scout 4 / chore 5 / general 25） |

所以问题不是"Ben 落后"，而是**Ben 把编排的全部智能压在 root 的 12 轮上，却没有给 root 一套可执行的调度原语**。

---

## 4. 差距评估（按影响排序）

### G1｜没有任务账本，也没有"认领"——重复劳动在花钱之后才被发现

- **Sapper**：`claim_intent` 在锁内把 intent 标成 `claimed + worker`，第二个 executor 直接 `FGError: claimed by ...`（`sapper.py:522-535`）；`auto` 每步只挑 `worker` 为空或自己的 intent（`sapper.py:3808-3811`）。
- **Ben**：任务只存在于 `create_agent(task="...")` 的自由文本里（`agents_graph/tools.py:496-557`，参数只有 name/task/skills/minutes/inherit_context）。共享账本 coverage **只能事后去重**：`_record_impl` 在写第二条时才比对 surface+risk_area 并拒绝（`coverage/tools.py:219-238`）——此时两个 agent 已经把同一面各测了一遍。
- **后果**：兄弟 agent 撞面。root 提示词只能写"Check existing agents to avoid overlap"（`root_agent.md:97-103`），这是**建议不是机制**。
- **可测量**：统计 `record_coverage` 返回 `existing_entry_id` 的次数 / 总 coverage 条目，即可估出重复率。

### G2｜frontier 只有"观测"，没有"就绪队列"

- **Sapper**：`frontier()` 只返回 ready（status open 且依赖满足），claimed 优先、其余按创建时间（`sapper.py:938-945`）；`pick` 直接打印 id 供脚本调用。
- **Ben**：注入块是三段：coverage gaps（`needs_follow_up`）、open todos、sub-agents 状态（`frontier.py:159-228`）。**没有 blocked/ready 区分，没有依赖边，没有优先级，没有"建议下一个任务"**。root 每轮要自己把这些碎片重新推成"下一步派谁"。
- **后果**：12 个决策轮里，有若干轮被"重新看懂现状"消耗；子任务粒度随 root 心情波动。

### G3｜没有 run 级"无进展"停机

- **Sapper**：图签名前后对比，不变即停（`sapper.py:3769, 3868-3870`）；另有 noop×2、异常×2 两个软停机。
- **Ben**：只有三类终止——`finish_scan`、`MaxTurnsExceeded`（12 轮）、`BudgetExceeded`（`runner.py:1154-1185`）。子 agent 层有 idle_resume 上限与 stall 通知（`execution.py:389-400`），但**root 层没有任何"世界没变"的检测**。
- **后果**：root 在预算耗尽前可以反复 `view_agent_graph`/`list_coverage` 空转；结束时 `stopped_reason` 也只有"到轮数/到预算"，无法区分"干完了"和"转晕了"。

### G4｜没有独立的验证关卡（fail-closed 的那一步）

- **Sapper**：`verify.md`（1,783 字节）输入只有 `intent_description` + `result_description`，输出 `verified/issues`；失败**写 hint 回图**，让下一轮 reason 看见（`sapper.py:3841-3866`）。verify 与 explore 用不同调用、不同上下文，等于"换双眼睛"。
- **Ben**：HTTP 类发现已有强证据门（exchange id 必须对得上，`reporting/tool.py:231-250`），这比 Sapper 强；但**非 HTTP 发现（配置、凭据、逻辑缺陷）没有对抗性复核**，验证只靠 root 提示词里的"Validation children"教条（`root_agent.md:112-118`），而且验证者常被要求"顺便也验一下"，容易继承 hunter 的确认偏误。
- **后果**：误报进入最终报告；或验证成本过高（继承上下文）。

### G5｜模型分层只有两档，没有"角色档位"和配额

- **Sapper**：三档（smart / flash / ds-flash）+ **按难度派单**（4 分钟 scout 用 flash 判定 easy/medium/hard，再决定士兵还是 general）+ **配额**（smart 只给四类岗位、并发 ≤3）+ **降级**（机械活让 flash 收尾，`SKILL.md:222-233`）。
- **Ben**：`root=LLM_MODEL`、所有 child=`BEN_FLASH_MODEL`（`factory.py:1200-1216, 1306-1333`）；`create_agent` 没有 tier/model 参数。
- **后果**：recon/校验这类窄任务与深度利用同档，要么浪费旗舰，要么深度活被 flash 拖住。这是**最大的成本结构问题**。

### G6｜没有 run 级 wall-clock，"残局收口"无处触发

- **Sapper**：`SAPPER_TOTAL_MINUTES` 全局时钟，每步把 `time_budget` 注入 prompt（`sapper.py:3764`）；board 心跳输出 `budget_left`；剩 20 分钟硬性停止 spawn（`SKILL.md:336-338`）。
- **Ben**：只有每个孩子的 45min 时限（`execution.py:441-462`）与美元/轮次预算；**没有"这次扫测还剩多少分钟"的概念**，也没有"最后阶段禁止开新战线"的硬约束。
- **后果**：战线铺开与收口时机全靠 root 直觉；尾部 run 容易"时间到了但报告没写完"。

### G7｜子 agent 的产出是"消息"，不是"图节点"，且没有 recall

- **Sapper**：executor 结束时 `conclude_with_new_fact` 写 typed fact（host/service/cred/vuln/loot/flag + data 键值），下一个 executor 的 prompt 里带 `recall_block`——按 title/description 的 token overlap + difflib 找 5 条相似节点（`sapper.py:1058-1097, 1208-1224`），无需 embedding。
- **Ben**：`agent_finish(result_summary, findings, open_items, ...)` 把文本报告塞进父收件箱（`agents_graph/tools.py:631-688`）；`open_items` 是自由文本，不落成 coverage 的 `ruled_out`；新 agent 派发时**不会自动带"历史上类似任务试过什么"**。
- **后果**：同一类死路被不同 agent 重走；root 上下文被 summary 文本占用。

### G8｜没有看护进程，也没有"一行式"心跳

- **Sapper**：watchdog 独立进程负责重启/轮换/到点收尾；`board` 一行输出 `budget_left` + `generals=` 供指挥官读（`worker.sh:656-665`）。
- **Ben**：有 child 崩溃恢复（`respawn_subagents`）、`-r` 续跑、operator pause 文件（`runner.py:1121-1136`）；但 root 卡在长调用上只有 stream timeout（idle 300s / total 600s），**没有独立看护者**；可观测性靠 TUI/viewer，没有结构化心跳文件。
- **后果**：长跑鲁棒性与可观测性偏弱（优先级最低，但排障时最痛）。

---

## 5. 优化方案

> 约定：**P0 = 一个 PR、纯增量、不改既有接口语义**；**P1 = 需要改 schema/CLI**；**P2 = 需要新增进程或批处理**。
> 所有百分比是估算，估算依据写在每项末尾；落地前建议先加 §6 的埋点，用真实基线校准。

### P0-1｜任务认领（claim）：把"避免重复"从事后判断改成事前占位

**优化目标**：任何"面 × 风险域"在开工前有唯一 owner；兄弟 agent 撞面时立即失败并被告知去找谁。

**改进点**
1. `Ben/core/ledger.py`（新，~120 行）：`claim(surface, risk_area, agent_id, ttl_min)` / `release` / `owner_of`；落盘 `run_dir/ledger.json`，`asyncio.Lock` + 原子写。
2. `Ben/tools/coverage/tools.py`：`record_coverage` 增加可选 `claim_id`；`_record_impl` 在**已有他人 claim 且未过期**时返回"已被 X 认领，去 `send_message_to_agent` 或换面"。
3. `Ben/tools/agents_graph/tools.py:create_agent`：task 里含 `claim_id` 时，spawn 前校验 owner；结束（`agent_finish` / 超时 / stop）自动 `release`，避免死锁（TTL 兜底）。
4. `root_agent.md` 的 "Avoid Duplication" 段从"建议"改成三步硬流程：`list_coverage` → 对空面 `claim` → 把 claim_id 写进 task。

**预期效果**：重复面 10~20% → 3~5%，等值节省 tokens 与 wall-clock 8~15%。
**依据**：coverage 现有 duplicate-rejection 计数可直接测量重复率；Sapper 用同一机制把并发 executor 的重复结果"在写 fact 前"丢弃（`sapper.py:492-499`）。
**风险**：claim 泄漏（agent 崩溃不释放）→ TTL 必须默认开启（建议 1.5× 该 agent 的 minutes）。

### P0-2｜无进展停机：run 级图签名

**优化目标**：连续 N 轮"世界没变"就强制收尾，而不是烧到轮数/预算上限。

**改进点**
1. `Ben/core/progress.py`（新，~80 行）：`signature() = (len(coverage), len(vulns), done_todos, 终态 agent 数, max(mtime of run_dir/*.json))`。
2. `ReportUsageHooks.on_llm_start`（`hooks.py:225`）：每轮前算签名，与上轮相同则 `stall_streak += 1`；`>= 3` 时注入 `[NO-PROGRESS]` 收尾指令（复用 `_ROOT_DIRECTIVES[2]` 的措辞），并置 `coordinator.stop_reason = "no_progress"`。
3. `runner.py:1163-1180` 的 `stopped_reason` 增加该分支，报告里如实写"因连续 N 轮无增量而收尾"。

**预期效果**：空转 run 提前 2~5 轮结束（12 轮制下即 15~40% 的轮次）；尾部 P90 成本 −25~35%。
**依据**：Sapper 的 `_graph_signature` 是纯本地计算、零模型调用；Ben 的签名同样只需读内存/文件。
**风险**：合法"长思考"被误杀 → 阈值 3 轮且只降级为"收尾指令"而非硬停；子 agent 在飞时不计 stall。

### P0-3｜角色档位（tier）：让 scout/validator 走便宜档

**优化目标**：模型花费按任务难度分配，而不是按"是不是 child"分配。

**改进点**
1. `create_agent(..., tier: Literal["scout","hunter","validator","general"] = "hunter")`。
2. `factory.build_agent` 增加 `tier` 参数 → 解析为 model：`scout/validator → BEN_FLASH_MODEL`（沿用现状）、`hunter → BEN_CHILD_MODEL`（新，默认同 flash 但可配）、`general → LLM_MODEL`（旗舰）。
3. 配额：`Ben/core/execution.py` 的 in-flight 检查旁加"general 并发 ≤1"（env `BEN_MAX_GENERALS`）。
4. `root_agent.md` 的升级阶梯表加一列 `tier`，与 `minutes` 一起下传。

**预期效果**：子 agent 成本 −15~30%（scout/validator 占 child 调用数的 30~50%，原本若走旗舰则单价差 5~10×）；质量不降（窄任务对模型不敏感），深度任务反而变强（general 拿到旗舰）。
**依据**：`factory.py` 已支持 `model_name` 覆盖（`:1200-1216`），只是没有"按角色选档"的路由；Sapper 的 mixed-elite 教条给出同构参数（smart 并发 ~3、其余 flash）。
**风险**：tier 误判导致深度活跑在 flash 上 → 升级阶梯里 `20-30min` 那一档显式要求 `tier="general"`，并在时间盒到点时提示 root 升级。

### P1-1｜就绪队列：让 frontier 输出"可派发的下一步"

**优化目标**：root 不再每轮从碎片里推导，注入块直接给"Ready now / Blocked / Claimed"三段 + 建议 task 骨架。

**改进点**
1. `Ben/tools/todo/tools.py`：todo 增加 `depends_on: list[str]` 与 `surface/risk_area` 字段。
2. `frontier.py:render_frontier_block` 改为四段：`Ready`（未认领、依赖满足，按 risk 排序）、`Blocked`（缺依赖，标出缺哪条）、`Claimed`（owner + 已耗时）、`Counts`（原三段保留）。
3. 每条 Ready 附一行 `suggest: name=... skills=[...] tier=... minutes=...`（由 risk_area→skill 的静态映射表生成），root 可直接抄进 `create_agent`。

**预期效果**：root 用于"看现状"的工具轮从 5~8 次降到 2~3 次（12 轮制下省 25~40% 的 root 轮次）；派发延迟下降。
**依据**：frontier 注入已有节流与 6KB 上限（`frontier.py:28-37, 254-277`），扩段不增加调用次数。
**风险**：块变长 → 保持 6KB 上限，Ready 段最多 8 条，其余折叠成计数。

### P1-2｜run 级时钟与残局收口

**优化目标**：给整次扫测一个 wall-clock，最后阶段禁止开新战线。

**改进点**
1. `settings.py`：`BEN_SCAN_MINUTES`（默认空=不限）。
2. `runner.py`：启动时算 `deadline`，传入 context；`hooks` 每轮注入 `[TIME] remaining=..m`，按 70/85/95% 复用现有三档收尾文案（`hooks.py:116-149`）。
3. **硬约束**：剩余 <20% 时 `create_agent` 直接返回错误（不是提示词），文案引导 `send_message_to_agent` / `finish_scan`。
4. 到点：`coordinator.cancel_descendants_graceful()` + 强制 `finish_scan`（复用 pause 的优雅停机制）。

**预期效果**：尾部 run 的"超时无报告"从偶发降到接近 0；报告完整率提升（估计 −20~40% 超时率）。
**依据**：Sapper 的 `budget_left` + "剩 20 分钟停止 spawn"是同构规则（`SKILL.md:336-338`）；Ben 已有 `_ROOT_DIRECTIVES` 三档文案可直接复用。
**风险**：硬拒绝 spawn 可能误伤临门一脚 → 白名单：`tier="validator"` 在最后阶段仍允许（验证既有发现，不开新面）。

### P1-3｜结构化收口 + recall：把"试过什么"变成可查节点

**优化目标**：死路不再重走；新 agent 派发自动带 3~5 条相似历史。

**改进点**
1. `agent_finish`：把 `open_items` 里带 `surface/risk_area` 的条目自动落成 `coverage(outcome="ruled_out", evidence=...)`；`findings` 仍走 vuln 报告（不重复）。
2. `create_agent`：spawn 前按 task 文本做 token-overlap（复用 Sapper 的 `_recall_tokenize` 思路，CJK 按字切分，`sapper.py:1013-1055`）在 coverage/notes 里找 top-3，拼进 task 的 "已知/已排除" 段（≤800 字符）。
3. `frontier` 的 `Claimed` 段显示每个在飞 agent 的 `minutes` 已用/剩余。

**预期效果**：每个 child 前 2~3 轮的重复探索 ↓ → child tokens −5~12%；root 收到的 summary 更短（结构化字段替代散文）。
**依据**：Sapper 的 recall 是纯本地字符串计算，零依赖零模型调用；Ben 已有 notes/coverage 两个可检索存储。
**风险**：recall 噪声 → 只取 top-3 且要求 overlap ≥0.34，宁缺勿滥。

### P2-1｜独立验证关卡（fail-closed）

**优化目标**：每条发现，在进入报告前被"另一双眼睛"尝试证伪；证伪失败自动降级。

**改进点**
1. 新工具 `verify_finding(finding_id)`：输入只有 `claim / evidence / falsifier`（"什么结果会推翻它"），`tier="validator"`、`inherit_context=false`、`minutes=10`；输出 `upheld | refuted | insufficient` + 理由。
2. `finish_scan` 前置：对 `status=unverified` 的发现批量跑一遍；`refuted` → 移入 `open_items` 并在报告里标"未证实"；`insufficient` → 保留但降置信度。
3. HTTP 类沿用现有 exchange-id 硬门（`reporting/tool.py:231`），不重复验证。

**预期效果**：误报率显著下降（行业基线 20~40% 的 AI 生成发现需人工否决，估可砍半）；代价 +3~5% 调用量。
**依据**：Sapper verify 模板仅 1.8KB、单次调用，成本远低于一条误报进入报告的代价。
**风险**：验证者也被说服（同模型同偏见）→ 强制 `inherit_context=false` + 只给 claim 与 falsifier，不给 hunter 的叙事。

### P2-2｜心跳与看护

**优化目标**：外部可观测 + root 卡死可自愈。

**改进点**
1. `runner` 每轮写 `run_dir/heartbeat.jsonl`：`ts / turn / remaining_min / spent_usd / in_flight / coverage_open / findings / stall_streak`；TUI 读它渲染一行状态。
2. `Ben/core/supervisor.py`：watchdog task，若 heartbeat 超过 `stream_total_timeout × 2` 未更新 → `cancel_descendants_graceful` + 以 `-r` 重启（复用现有 resume 路径）。
3. 预算/时间到点由 supervisor 兜底强制收尾（对齐 Sapper buzzer）。

**预期效果**：长跑失败可自愈；排障从"翻日志"变成"看一行"。
**依据**：Sapper watchdog 的职责边界很窄（重启/轮换/收尾），Ben 的 runner 已有 resume 与 graceful cancel 原语，只缺触发者。
**风险**：误重启 → 只在"心跳停更"且"无在飞 LLM 调用"时触发。

---

## 6. 实施顺序与验证

**阶段划分**

| 阶段 | 内容 | 改动面 | 依赖 |
|---|---|---|---|
| P0（1 个 PR） | claim / no-progress / tier | 新增 2 文件 + 3 处小改 | 无 |
| P1 | 就绪队列 / run 时钟 / 结构化收口+recall | todo schema、frontier 渲染、settings、CLI | P0-1（claim 是 Ready 段的前提） |
| P2 | 验证关卡 / 心跳+看护 | 新工具、新 supervisor task | P1-2（时钟是兜底前提） |

**先埋点，再动手**（半天，只读）
1. 一次 20-turn 基线 run，记录：`coverage` 重复拒绝次数、root 工具调用分布（`report/usage.py` 已分账）、`stall` 相关日志、每 child 的 `minutes` 实际用满率。
2. 用这些数字校准 §5 的百分比；若重复率 <5%，P0-1 降级为 P2。

**验收指标（每项优化都要能证伪）**

| 优化 | 指标 | 期望方向 |
|---|---|---|
| P0-1 claim | 重复面数 / 总面数 | ↓ 至 <5% |
| P0-2 no-progress | 空转轮数 / run；`stop_reason` 分布 | 空转 ≤1 轮 |
| P0-3 tier | child 成本 / 总成本；general 任务成功率 | 成本 ↓15~30%，成功率不降 |
| P1-1 就绪队列 | root 每 run 的 `list_*`/`view_agent_graph` 次数 | ↓30~50% |
| P1-2 时钟 | 超时 run 占比；报告完整率 | 超时 ↓，完整率 ↑ |
| P1-3 recall | child 首 3 轮 token；重复死路数 | 双降 |
| P2-1 verify | 人工否决率 | ↓50% |
| P2-2 心跳 | MTTR；异常 run 自愈率 | MTTR ↓ |

**回滚策略**：全部走 env 开关（`BEN_CLAIM=0/1`、`BEN_NO_PROGRESS_TURNS=0`、`BEN_SCAN_MINUTES=`、`BEN_MAX_GENERALS=`），默认值保持现有行为，灰度后再切默认。

---

## 7. 一页对照表

| 能力 | Sapper | Ben 现状 | 差距性质 | 方案 |
|---|---|---|---|---|
| 共享事实 | typed fact graph，文件 + flock | coverage/notes/vulns（结构化，进程内锁） | **Ben 不弱** | — |
| 任务认领 | claim 锁内唯一 owner | 无（事后去重） | 缺机制 | P0-1 |
| 就绪队列 | frontier 只出 ready，DAG 边 | gaps+todos+agents 平铺 | 缺调度语义 | P1-1 |
| 空转停机 | 图签名 | 无 | 缺硬规则 | P0-2 |
| 验证关卡 | 独立 verify 调用 + hint 回图 | HTTP 证据门（更强）+ 无对抗复核 | 半边缺失 | P2-1 |
| 模型分层 | 3 档 + 角色配额 | 2 档，无角色 | 缺路由 | P0-3 |
| 总时钟 | 470min + budget_left + 20min 停派 | 无 run 级时钟 | 缺预算 | P1-2 |
| 交接/续跑 | STATE 文档 + `--continue` | `-r` + 已排除面注入 | **Ben 不弱** | — |
| 自愈 | watchdog（重启/轮换/buzzer） | child 恢复 + 无 supervisor | 缺看护 | P2-2 |
| 可观测 | board 一行心跳 | TUI/viewer，无结构化心跳 | 缺聚合 | P2-2 |

---

## 8. 证据索引

**Sapper**
- `lib/sapper.py:181-188` flock；`:479-520` conclude_with_new_fact 原子事务；`:522-547` claim/release
- `lib/sapper.py:833-847` is_ready（DAG 依赖）；`:938-945` frontier；`:1151-1183` 图渲染（三层记忆）
- `lib/sapper.py:1186-1224` render_prompt / render_intent_prompt（recall_block）
- `lib/sapper.py:1013-1097` recall 的 tokenize/score/render
- `lib/sapper.py:2647-2723` cmd_sweep；`:3727-3874` cmd_auto（五个停机条件）；`:3932-3939` graph signature
- `docker/commander/SKILL.md:41-67` 七步循环；`:178-195` 升级表；`:209-235` 模型分层与配额；`:264-279` 85/90 压缩协议；`:322-338` 半场/残局
- `docker/commander/worker.sh:242-253` mission 模板；`:536-665` board + 心跳
- `docker/commander/watchdog.sh:289-293` cwd 作用域 --continue；`:380-395` relaunch；`:636-651` 重启上限与 stall 不干预
- `docker/commander/SPEC.md:23-29` 环境与模型分层；`:26` 3 实例上限

**Ben**
- `Ben/core/execution.py:441-462` 子 agent 时限；`:465-554` spawn（in-flight 闸、无 task 去重）
- `Ben/core/hooks.py:225-263` 每轮注入；`:116-149` 三档收尾文案；`:358-401` 预算分档
- `Ben/core/frontier.py:159-228` 注入块构成；`:254-277` 签名节流；`:45-91` resume 已排除面
- `Ben/core/runner.py:820` max_turns；`:1154-1185` 终止原因
- `Ben/config/settings.py:245-249` max_turns 默认 12
- `Ben/tools/agents_graph/tools.py:496-557` create_agent 参数；`:631-688` agent_finish
- `Ben/tools/coverage/tools.py:191-256` 事后去重
- `Ben/tools/reporting/tool.py:231-250` HTTP 证据门
- `Ben/skills/coordination/root_agent.md:72-118` 每轮纪律/升级阶梯/避免重复
