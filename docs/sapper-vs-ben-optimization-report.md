# Ben × Sapper 对比分析与优化方案

> 对比对象：`/home/kali/Desktop/AI/BenProject`（Ben，授权范围漏洞测试 agent，OpenAI-agents SDK + Docker 沙箱）
> 参照对象：`/home/kali/Desktop/AI/sapper-v2`（多 agent 渗透编排框架，腾讯攻防挑战赛冠军，1B token 胜 4.5B）
> 日期：2026-11-16。所有"预计收益"为基于代码事实的估算，已标注估算依据；sapper 的 "1B vs 4.5B" 是 README 宣称值，代码中无 token 计量。

---

## 0. 一句话结论

**Sapper 用"结构"省 token：小而分层的共享记忆、分钟级升级阶梯、smart 模型只花在四类岗位、一切脚本幂等。**
**Ben 的基础设施更现代（SDK 会话、自动 compaction、Claude prompt caching、Docker 隔离、高保真 resume），但缺"调度纪律层"和"复用闭环层"**——并发上限是终身总量而非在飞上限、没有 per-task 时限、recon/scout 产出是死胡同、resume 只防重复报告不防重复测试、RAG 全量重建、tool 输出溢出无去重。

| 维度 | Sapper 做法 | Ben 现状 | 差距方向 |
|---|---|---|---|
| 上下文管理 | 三层记忆（title/description/doc），prompt 仅注入前两层 | 全量 SDK 会话 + compaction + output spill | Ben 有 compaction，Sapper 有结构化分层 → 可互补 |
| token 经济 | smart/flash 双档 + smart 并发≤3 + 每迭代至多 2 个决定 | root/child 双档，但无岗位级配额 | Ben 缺纪律，不缺机制 |
| 任务拆分 | fact-intent DAG + frontier + claim + reason→explore→verify | 纯涌现：root prompt 政策 + create_agent | Ben 缺共享任务图 |
| 执行效率 | 升级阶梯（分钟表）、scout 4 分钟、park at 45min | BEN_MAX_SUBAGENTS=4（终身总量）、仅 per-call 超时 | Ben 缺 per-task 时限与在飞并发闸 |
| 代码结构 | 4,726 行单文件 stdlib + markdown 模板 + tmux 总线 | 45K 行 / 144 文件 / 23 依赖，与 strix 大面积同源 | 各有合理性；Ben 可借"策略=markdown、手=脚本"分层 |
| 缓存/增量 | **无 prompt cache**；靠幂等种子/claim/签名去重避免重算 | 有 Claude-only prompt cache；但 recon 死胡同、RAG 全量重建、spill 无去重 | Ben 在 provider 缓存上领先，在**复用闭环**上落后 |

---

## 一、上下文管理与 token 消耗

### 1.1 共享记忆的三层化 vs 全量会话回放

**Sapper 做法**（`lib/sapper.py`）：
- 每个节点一个 JSON：`title` / `description` / `doc` 三层。`_to_yaml_like`（约 1255-1298 行）**只把 title+description 序列化进 prompt**，`doc` 仅标记 `(has doc)`，模型需要时自己调 `sapper doc <id>` 取。
- 实测：每个节点进 prompt 约 250 字符；30 facts/12 intents 的 commander prompt 约 13.7K 字符；一轮 executor 全开销约 9.4K 字符。**上限是结构性的，不是截断性的**。
- 附带防注入：`_scrub_inline` 剥控制字符、`_prompt_inline` 剥反引号防 fence 逃逸；占位符单遍 `re.sub` 替换，防止 fact 文本重写后续占位符。
- 会话饱和无代码计量：靠 harness `autoCompactEnabled` + doctrine 的 85/90 协议（ITERATIONS≥8 或上下文提示即写 `commander-state.md`，<150 行）+ watchdog `--continue` 重启 + `MISSION_B64` 重播任务书。换人成本 = 重读 150 行状态文件。

**Ben 现状**：
- 历史在 SDK `SQLiteSession`（`strix_runs/<run>/.state/agents.db`），无应用层消息列表。
- 已有**分层 compaction**（`Ben/llm/compaction.py`，411 行）：主动（每 cycle 前）+ 被动（context overflow 重试 ≤2 次/周期）；旧工具输出先截到 2K 字符再摘要；摘要本身用 flash 模型（分层正确）；keep 8K / buffer 20K / summary 4K。这是 Sapper 没有的。
- 系统提示较大：`system_prompt.jinja` 28.8KB（404 行）+ 自动内联 fragment，child 约 35-40KB ≈ **12-18K tokens 静态开销**；Sapper executor 每轮全开销才 ~9.4K 字符。
- `load_skill` 按需加载已有预算控制（≤5 个 / ≤24K 字符/次）——与 Sapper 的 `doc` 按需读取同构，方向正确。

**优化建议 A1：给"已完成子 agent 的会话"加第二压缩层**
- child `agent_finish` 已产出 `result_summary/findings/open_items`，但完整会话仍留在 agents.db 且 root 的 mailbox 消息是自由文本。建议：`agent_finish` 时把 child 会话蒸馏为结构化 fact（surface / outcome / evidence 路径），root 侧只保留三层视图（标题行 → summary → 按需引用 spill 文件），对应 Sapper 的 title/description/doc。
- **预计收益**：长 run（>40 turns）中 root 上下文里"已完成 child 的遗留消息"通常占 20-40%；三层化后这部分降到 <5%。以一个 deep scan（root ~60 轮、每轮输入 30-60K tokens）估算，约省 **15-30% 总输入 token**；同时降低 compaction 触发频率（每次 compaction 本身要花一次 flash 摘要调用）。

**优化建议 A2：系统提示瘦身审计**
- 对照 Sapper 的"固定指令占 prompt 30-55%"红线，对 `system_prompt.jinja` + 自动内联 fragment 做一次 token 审计：哪些段落在 90% 的 run 里从未被用到 → 降级为 `load_skill` 按需片段。84 个 skill 共 728KB，`llm_applications.md` 23.7KB 这类已按需，但 jinja 本体 28.8KB 值得逐段过。
- **预计收益**：child 每轮输入省 3-6K tokens（静态部分降 20-30%）。对 20-turn quick scan × 4 agents，约省 **0.5-1.5M tokens/run 量级的输入账面**；Claude 路由下因前缀命中缓存，实际账单收益折半，非 Claude 路由全额定格。

### 1.2 模型分层的"岗位配额"纪律

**Sapper 做法**（`docker/commander/SKILL.md` §4.9）：
- smart（GLM-5.3）只准花在四类岗位：攻坚 general、难判定 general、中场审计、终局重开；**并发 smart pane ≤3**（"glm 并发是最稀缺资源"）；其余一律 flash。worker.sh 按 `--gw glm|ds` 落盘 pane-env，watchdog 重启保档位。
- 这被作者自述为 1B 胜 4.5B 的第一经济杠杆。

**Ben 现状**：
- 已有 root=旗舰 / child=flash 二档（`BEN_FLASH_MODEL`），且 flash 还复用于 compaction 摘要、报告去重——机制上是对的。
- 缺失：无并发配额语义（见二.2）；root 自己的轮次没有"每迭代至多 2 个决定 + sleep"式的节流——root 是旗舰模型，每一轮犹豫都在烧最贵的档位。

**优化建议 A3：root 轮次纪律 + spawn 档位政策**
- 在 `coordination/root_agent.md` fragment 中写明：root 每轮输出应收敛为"读状态 → 至多两个决定（spawn/msg/stop）→ 交给子 agent"，禁止长篇分析；spawn 时默认 flash，只有明确"攻坚/复核"类任务才允许继承旗舰档（需要一个 `tier` 参数传给 `create_agent`）。
- **预计收益**：root 输出 token 通常占 run 总输出的 10-20%（旗舰单价往往是 flash 的 5-10 倍）。收敛 root 行为 + 攻坚类 spawn 占比 <20% 时，加权模型成本估算可降 **20-40%**（Sapper 的四岗位配额即此逻辑的赛场上限验证）。

### 1.3 prompt caching：Ben 领先，但覆盖面窄

**Sapper**：完全没有（无 cache_control、无稳定前缀构造；commander 每次重启重读 24KB SKILL.md）。**这是 Ben 明确领先的一项，不必学。**
**Ben 现状**：`<cache_point>` 分割稳定前缀 + LiteLLM `cache_control_injection_points`，仅 `is_claude_model ∧ routes_through_litellm` 生效；`Ben/report/usage.py` 已区分 cached_tokens 计价，报表已能观测命中率。
**优化建议 A4**：对 DeepSeek（context caching 自动按前缀命中，cache hit 价格 ~1/10）与 OpenAI（自动前缀缓存）路由，做一次"前缀稳定性验证"——确保 system prompt + 首轮工具结果顺序在多轮间字节级稳定（cache_point 之后的 requested skills 段可能破坏 DeepSeek 的自动前缀命中）。**预计收益**：非 Claude 供应商路由下输入成本降 **50-80%**（DeepSeek cache-hit 计价），零代码改动、只需排序保证 + 观测确认。

---

## 二、任务拆分与执行效率

### 2.1 显式任务图 vs 涌现式拆分

**Sapper 做法**：
- fact-intent DAG：intent 有 `from`（依赖 facts）和 `to`（产出 fact）；"ready" 由结构推导（`frontier()`），不需要状态机。`claim/release` 防两个 worker 抢同一 intent；`conclude_with_new_fact` 在单锁内原子写 fact + 回链。
- `auto` 循环五停机条件（deadline / goal / reason 连续 noop×2 / API 失败×2 / 图签名无变化）；`_graph_signature` 元组快照防止空转烧 token——**"一步没改变图就停"是最便宜的正确性闸门**。
- 失败的 verify 会写成 hint 回注图，下一轮 reason 看得见——失败可继承。

**Ben 现状**：
- 拆分纯涌现：root prompt 政策 + `create_agent`；todo 工具是平的（无依赖边）；coverage ledger 拒绝重复 (surface, risk_area) 且有 outcome 生命周期——**这已经是半个 fact 图，缺的是依赖边和 frontier 视图**。
- `wait_for_agents` 有"同轮第二次 wait 拒绝"防呆；mailbox 点对点。
- 无任何"这一步没产出就停"的信号；soft turn limit 只警告不停。

**优化建议 B1：把 coverage+todo 升级为 root 每轮注入的 frontier 视图**
- 学 `sapper-hook.py` 的做法：每轮给 root 注入一个 ≤6KB 的紧凑状态块（8 条最新 fact 级信息 + 4 条未决 intent + coverage 缺口），标题行 + 90 字符摘要。数据源全部现成（`.state/coverage.json`、todos、mailbox），只差一个渲染器。
- **预计收益**：root 目前要靠主动调 `list_coverage`/`view_agent_graph` 才知道全局（每次调用本身是一轮旗舰 token + 结果入上下文）。状态注入替代后，**每个 run 省 root 侧 5-15 次工具调用轮**（≈ 每轮 30-60K 输入 token），同时降低"root 不知道 children 已测过什么"导致的重复 spawn（这是效率收益的大头）。

### 2.2 并发闸与 per-task 时限

**Sapper 做法**：
- pane 上限 8（廉价）、平台实例上限 3（昂贵），同一题叠 pane 不占实例——**资源分档计数**。
- 升级阶梯（SKILL.md §4.6，含预算算术 "24 slot-hours / 63 题 ≈ 23 分钟/题"）：0-10min 无进展 → `msg`（免费）；10-20 → spawn w2 换视角；20-30 → spawn general（**唯一默认 smart 花费**）；30-45 → park 止损。
- scout 4 分钟定性（easy→soldier / hard→general），同前缀题目跳 scout 直接用兄弟图的情报简报（isomorphism transfer："被简报的 worker 5 分钟解家族变体，未简报的从零开始"）。

**Ben 现状**：
- `BEN_MAX_SUBAGENTS=4` 是**终身总量闸**不是在飞上限——代码数的是"所有非 root agent 总数"，spawn 满后连替换死掉的都不行；无 semaphore、无队列。
- 无 per-task wall-clock：只有 per-call 超时（命令 30s、wait 300s、流 600s）。一个迷路的 child 会烧完自己 12 turns（MAX_AGENT_TURNS）才停。
- 有现成的 `recon.py`/`scout.py`（免模型、免 run 记录，scout 甚至能 `--emit sapper` 输出 facts！）但**零消费者**——扫描必须用模型 token 重新发现目标结构。

**优化建议 B2（P0）：BEN_MAX_SUBAGENTS 改为在飞 semaphore + per-task 时限**
- 语义改为"同时在飞上限"，完成/失败的 agent 释放配额；`create_agent` 增加 `minutes` 参数，`_child_loop` 里用 `asyncio.wait_for` 到时触发 graceful park（复用 `stop_agent` 的 cascade 逻辑）。
- **预计收益**：防止"4 个名额被早完成的僵尸计数占满"导致的整 run 停摆（正确性修复）；per-task 时限把迷路 child 的浪费从"最多 12 轮 flash"封顶到"N 分钟"，按 Sapper 经验（park at 45min 的止损表），深扫 wall-clock 预计缩短 **15-25%**。

**优化建议 B3（P0）：recon/scout 产出接入扫描（幂等种子）**
- 对应 Sapper `_bench_prepare_graph`：开跑前把 `scout.json`/`facts.jsonl` 的 host/service/version/难度判定作为种子 fact 写进 root 初始任务与 workspace，幂等（同 kind+data 跳过）。代码上 scout 已能 emit，只差 `prepare_run()` 里的桥接与 root task 模板的一段注入。
- **预计收益**：典型扫描前 3-5 轮（结构发现）由模型轮变为零 token 的 CLI 预处理；以 quick scan 20 turns 计，**每 run 省 ~15-25% 输入 token + 数分钟 wall-clock**。这是全部建议里性价比最高的一条。

**优化建议 B4：root prompt 写入升级阶梯与止损表**
- 把 §4.6 的分钟表（0-10 msg / 10-20 换视角 / 20-30 升级 / 30-45 park）翻译成 Ben 的 `create_agent`/`send_message_to_agent`/`stop_agent` 语义，写进 `coordination/root_agent.md`。纯 markdown 改动，注意 `tests/test_root_prompt_policy.py` 与 `_ROOT_SKILLS` 的测试耦合。
- **预计收益**：无新机制成本；减少"卡住的 child 烧满 12 轮才自然死亡"和"root 无限 msg 轰炸"两类浪费，估算深扫无效 token（花在无进展分支上的）降 **20-30%**。

---

## 三、代码结构与复用策略

**Sapper 做法**：
- 单文件 4,726 行、零三方依赖、每节点一 JSON、`fcntl.flock` + `os.replace` 原子写、git-diffable。坏文件跳过不崩（`_read_node_files` 容错）。
- **"策略=markdown、手=脚本"分层**：426 行 doctrine 是给模型读的；watchdog 零策略只保活（重启/换 key/收尾）。SPEC.md 把五个 live bug 写成 do-not-regress 陷阱清单。
- 模板 = 裸 markdown + 单遍占位符替换，六个角色模板合计 ~22K 字符。
- agent 总线就是 `tmux send-keys -l` + `capture-pane` + 死亡哨兵 `DRIVE_PANE_EXITED`——无框架。

**Ben 现状**：
- 45,243 行 / 144 文件 / 23 直接依赖；最大单模块 `tools/reporting/tool.py` 2,428 行。多接口（TUI/web/CLI）+ Docker 沙箱 + 授权闸门，复杂度有正当性，**不必学单文件**。
- 与 strix 大面积同源（todo/coverage 工具 byte-identical，117 处"对齐 strix"注释）是刻意的移植策略，保持 diff 可追溯——这本身是一种复用策略，代价是与上游演进解耦困难。
- **langchain-core + langchain-text-splitters 两棵依赖树只为 281 行 RAG 代码服务**（splitting + embeddings）；向量检索是全量载入 Python 的暴力余弦。

**优化建议 C1：RAG 去 langchain**
- Sapper `kb.py`（token 重叠打分 + CJK 逐字切分 + 5 条上限）证明了这种场景不需要向量库。用 stdlib 替换 `RecursiveCharacterTextSplitter`（同款分隔符列表 30 行可写）+ 保留可选 OpenAI embeddings，或干脆换 hashing trick（Ben 的 LocalEmbeddings 已经是）。
- **预计收益**：依赖树 -2 个直接项（传递依赖约 -30 包），安装/冷启动变快、供应链面缩小；`ingest` 从"删库全量重嵌"改为按 chunk sha 增量（见 D2）。
**优化建议 C2：output_store 内容寻址去重**
- `bound_and_store` 现在每次溢出都写新 UUID 文件；改为 sha256 内容寻址 + 命中复用路径。10 个 agent 跑同一条 nmap 的场景从 10 份文件变 1 份。
- **预计收益**：沙箱磁盘与写入 IO 降低；更重要的是提示里引用的是稳定路径，agent 间可以互认"同一个输出文件"，减少重复读取（每省一次全量 cat ≈ 省一次 50KB 级工具结果入上下文 ≈ ~15K tokens）。

---

## 四、缓存与增量处理

**Sapper 做法**（无 provider 缓存，全靠"不重算"）：
- 幂等种子、`claim` 防抢、`_graph_signature` 防空转、explore 幂等（已派发过直接跳）、flag 台账状态机（recorded→submitted/rejected，重复提交=成功吸收）、`--close` 兄弟存活守卫、key 轮换风暴闸（5 分钟冷却 + 6 次/小时上限，来自真实事故）。
- kb 56 条 / 52KB，**两段式检索**：`kb search` 只给 id+标题+100 字符描述，`kb show <id>` 才给全文——与三层记忆同构。
- Claude hooks 每轮注入状态但硬上限 6KB。

**Ben 现状（缺口清单）**：
1. resume 保真度高（agents.db 全历史回放、todo/notes/coverage/threat-model 全水合），但**只防重复报告（LLM 去重），不防重复测试**——恢复的 run 会把 ruled_out/no_issue_found 的 surface 全部重测一遍。
2. recon/scout 死胡同（见 B3）。
3. RAG `ingest` 签名不匹配即 `DELETE FROM chunks` 全量重嵌；`search()` 每次重嵌 query（API embeddings 下是真金白银）。
4. output spill 无去重（见 C2）。
5. coverage 有 `gaps` 机制但只靠模型自觉调 `list_coverage`，没有程序化 frontier（见 B1）。

**优化建议 D1（P1）：resume 时注入"已测面"抑制重测**
- 水合时把 coverage 里 `ruled_out` / `no_issue_found` / `not_applicable` 的 surface 列表写进 root 的 `resume_instruction`（现有高优先级注入通道），配一句"以下面已在前次 run 中排除，除非有新证据不要重测"。对应 Sapper"图即状态，接替者读图不重推"。
- **预计收益**：中断后续跑场景省去重测面，估算输入 token 省 **30-50%**（视前次进度），且 wall-clock 显著缩短。改动集中在 `Ben/core/runner.py` 的 resume 分支。

**优化建议 D2：RAG 增量 ingest + query 记忆化**
- chunk id 已是 `sha256(metadata+content)`，改成"存在即跳过"，只删消失的文档的 chunk；`search()` 对 query embedding 加 LRU（API embeddings 场景）。
- **预计收益**：语料微调后 ingest 从全量（656KB 语料 + API embedding 费用）变增量；重复检索零成本。

---

## 五、按优先级排序的落地实施方案

| # | 优先级 | 改动 | 文件落点 | 预计收益（估算） | 成本/风险 |
|---|---|---|---|---|---|
| 1 | **P0** | recon/scout 产出幂等种子进 scan | `Ben/interface/cli/scan_setup.py:prepare_run()` + root task 模板；消费 `scout.json`/`facts.jsonl` | 每 run 省 15-25% 输入 token + 数分钟；免模型侦察 | 低；scout emit 已存在，纯桥接。注意白名单同闸门 |
| 2 | **P0** | 并发闸改在飞上限 + per-task 时限 | `Ben/core/execution.py:spawn_child_agent/_start_child_runner`；`create_agent` 加 `minutes` | 修"名额被终身占用"停摆；深扫 wall-clock -15-25% | 低-中；需补 tests/test_session_fd 类并发测试 |
| 3 | **P0** | root 升级阶梯/止损/每轮两决定纪律 | `Ben/skills/coordination/root_agent.md`（纯 fragment） | 无效分支 token -20-30%；root 输出收敛 | 很低；过 test_root_prompt_policy.py |
| 4 | **P1** | root 每轮注入 ≤6KB frontier/状态块 | 新渲染器 + runner 注入点（学 sapper-hook） | 每 run 省 root 5-15 次工具调用轮；减少重复 spawn | 中；注意与 compaction 的交互（注入消息应可丢弃） |
| 5 | **P1** | resume 注入"已测面"抑制重测 | `Ben/core/runner.py` resume 分支 + coverage 水合 | 续跑输入 token -30-50% | 低-中；需防止模型盲信旧结论（保留"新证据可推翻"措辞） |
| 6 | **P1** | child 会话三层化（finish 蒸馏 fact） | `agent_finish` 落点 + mailbox 渲染 | 长跑 root 上下文 -15-30%；compaction 频率降 | 中 |
| 7 | **P1** | output spill sha256 去重 | `Ben/tools/output_store.py` + runner spill writer | 磁盘/IO 降；跨 agent 复用大输出（每次省 ~15K tokens 级读取） | 低 |
| 8 | **P2** | 非 Claude 路由前缀稳定性验证（DeepSeek/OpenAI 自动缓存） | `Ben/core/inputs.py` 请求组装顺序 + 观测 | 非 Claude 路由输入成本 -50-80%（计价层面） | 低；主要是验证与排序保证 |
| 9 | **P2** | spawn 档位参数（攻坚类才允许旗舰） | `create_agent` + factory | 加权模型成本 -20-40%（配合 #3） | 中 |
| 10 | **P2** | RAG 去 langchain + 增量 ingest + query cache | `Ben/rag/*` | 依赖 -2 直接项；ingest 增量化 | 中；行为等价测试要先行 |
| 11 | **P2** | 系统提示瘦身审计 | `system_prompt.jinja` + fragment 清单 | child 每轮静态输入 -20-30% | 中；prompt 测试耦合多，需逐段过 |

**实施节奏建议**：#1-#3 一个 PR（全部低风险高收益，先拿下"免费"部分）；#4-#5 第二个 PR（复用闭环，共享"状态注入"基础设施）；#6-#7 第三个；#8-#11 按观测数据挑着做——先给 `Ben/report/usage.py` 的 cached_tokens 命中率加一个 run 级汇总指标，用真实数据决定 #8/#11 的优先次序。

**不要照搬的部分**：Sapper 的 tmux 总线（Ben 的 asyncio+SDK 在同进程语义下更正确）、单文件架构（Ben 的复杂度有正当性）、无 token 计量的"分钟预算"（Ben 的美元预算 + usage 报表更精确，反而应保留并强化）。

---

### 附：关键证据索引（抽查用）

- Sapper 三层序列化：`lib/sapper.py` `_to_yaml_like`（docstring 自述"NOT doc … so prompts stay small"）
- Sapper 升级阶梯/四岗位配额：`docker/commander/SKILL.md` §4.6（178-195 行）、§4.9（209-235 行）
- Sapper 停机条件与图签名：`lib/sapper.py` `cmd_auto`（3727-3874）、`_graph_signature`（3932）
- Ben compaction 参数：`Ben/llm/compaction.py:34-36`、`Ben/config/settings.py` ContextSettings
- Ben prompt cache：`Ben/agents/prompt.py:47` CACHE_POINT、`Ben/core/inputs.py:317` `_prompt_cache_extra_args`
- Ben 并发闸（终身总量语义）：`Ben/core/execution.py:451-470`
- Ben recon 死胡同：`grep recon.json Ben/` 仅命中写方 `Ben/interface/cli/recon.py`
- Ben RAG 全量重建：`Ben/rag/vector_store.py`（签名不匹配 `DELETE FROM chunks`）
- Ben resume 只防重复报告：`Ben/report/dedupe.py` + `.state/coverage.json` 无消费者抑制逻辑
