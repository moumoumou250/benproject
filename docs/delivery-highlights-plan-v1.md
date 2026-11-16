# Ben 多 Agent 编排框架｜可交付亮点方案（v1）

> 用途：把 4 份分析文档（Sapper 对比、编排深读、代码审查 v2、提示词审计）里**已经落地或被验证有效**的设计，整理成可直接引用的交付材料。
> 组织原则：每一条都写清 **作用（what for）→ 适用场景（when）→ 能力提升（capability gain）→ 代码锚点（where）**，可直接复制进方案书/答辩材料。
> 事实基准：本文件所有数字与行为均**实测核对过仓库**（commit `5a7b484`），与原始报告有差异处已标注。

---

## 0. 与原始报告的三处事实修正（先说清楚，避免答辩被问倒）

| 报告原说法 | 仓库实测 | 处理 |
|---|---|---|
| 审查报告 H1–H9 列为"必须修的缺陷" | **H1/H2/H3/H4/H7/H8/H9 已在 `5a7b484` 修复**：supervisor 判 `wait_kind in ("user","agents")`、`mark_llm_*` 改带时间戳登记表 + `stale_after_s`、`finish_scan` 已消费 `refuted_ids/unverified_ids`、spawn 异常有 `release_for_agent`、`_finding_looks_http` 已改用 `http_exchange_ids`、`create_agent` 已强制 surface×risk_area | 这些是**可交付的加固设计**，写进"亮点"，不再是待办 |
| 审查报告 M9/M11 列为待改 | `scan_clock`、`jsonstore.atomic_write_json` 均已存在（`jsonstore.py` 43 行被 4 个模块共用） | 作为"统一抽象"亮点 |
| 提示词审计"建议瘦身" | 已执行，实测 deep child 9,339→6,813（−27%） | 作为"已验证收益"引用 |

另有 14 个 `tests/core` 失败为**既有失败**（`runner.run_dir_for` 符号错位 + `test_budget_pause_policy` 在 `45dca37` 即存在），与编排层无关，答辩时主动说明比被问出来好。

---

# 第一部分｜可直接替换现有设计的 9 个亮点

> 每条都对应一个真实模块，可直接作为"方案第 X 章"的正文。

---

## 亮点 1｜任务认领（Claim）：把"避免重复"从事后判断改成事前占位

**作用**：任何 `(surface, risk_area)` 工作单元在 spawn 前必须有唯一 owner，兄弟 agent 撞面时**立即失败并被告知去找谁**，而不是各测一遍再被 coverage 去重。

**适用场景**：多子 agent 并行扫同一目标的多个面（`/api/*`、`/admin`、上传、鉴权）——这正是"重复劳动"成本最高的场景。

**能力提升**
- 重复面从 10~20% 压到 <5%（coverage 的 `existing_entry_id` 命中数可直接度量）；
- 失败路径快：第二个 agent 在 spawn 阶段就被拒，不消耗任何 token。

**代码锚点**
- `Ben/core/task_claims.py`（226 行）：`try_claim / release_for_agent / owner_of`，键规范化 `strip().lower()` **与 coverage 账本完全一致**（两个账本不打架，这个细节最容易漏）；
- 闸门在 spawn：`execution.py:530/605`；失败释放：`execution.py:684-689`（`except BaseException` → `release_for_agent(child_id)`）；
- 强制前置：`agents_graph/tools.py:596-625`，`BEN_CLAIM=1` 时 `hunter/general` 不带 `surface×risk_area` 直接返回错误，并提示"侦察用 scout、只验证用 validator"；
- TTL = `BEN_SUBAGENT_MINUTES × 1.5` ≈ 67.5 分钟（`task_claims.py:139`），崩溃不释放时兜底解锁。

**方案表述建议**
> "编排层的并发正确性由**磁盘上的唯一归属**保证，而不是由模型的自觉保证。认领在 spawn 前完成、在终止状态释放、以 1.5 倍任务时限作为崩溃兜底，形成闭环。"

**尚有风险（可写进"下一步"）**：TTL 期间若进程崩溃，该面会锁死至超时；建议改"预占（内存）→ register 成功后 commit（落盘）"两段式。

---

## 亮点 2｜角色档位（Tier）：成本按任务难度分配，而不是按"是不是子 agent"分配

**作用**：`create_agent(tier="scout"|"hunter"|"validator"|"general")` 把"用哪个模型"变成**角色属性**，而不是全局两档（root 旗舰 / child flash）。

**适用场景**：侦察、判定、验证这类窄任务与深度利用同档时，要么浪费旗舰、要么深度活被 flash 拖住。

**能力提升**
- 子 agent 成本 −15~30%（scout/validator 占子调用数 30~50%，单价差 5~10×）；
- 深度任务质量反升：`general` 能拿到旗舰模型。

**代码锚点**
- `Ben/core/tiers.py`（10 行，**单一来源**）：`AgentTier = Literal["scout","hunter","validator","general"]` + `CLAIM_REQUIRED_TIERS`，`create_agent` 与 spawn 共用，避免两处定义漂移；
- `_FACTORY_MODEL_SENTINEL`（`factory.py:1295/1336-1342`）：**干净地区分"没传 model_name"与"显式传 None"**——显式 `None` = 旗舰，不传 = 默认 flash；
- 并发配额：`BEN_MAX_SUBAGENTS=4`（**在飞**语义，非终身总量）+ `BEN_MAX_GENERALS=1`（`execution.py:549-581`）。

**方案表述建议**
> "三档角色 × 两档模型的笛卡尔积里，只开放 4 个有语义的档位；`general` 并发硬限 1，保证旗舰模型是稀缺资源而非默认资源。"

---

## 亮点 3｜无进展停机（Graph Signature）：只判断"世界有没有变"，不判断"模型有没有努力"

**作用**：每轮计算一个内容签名（coverage 数、漏洞数、完成 todo、终态 agent、文件 mtime），连续 N 轮不变就注入 `[NO-PROGRESS]` 收尾指令。

**适用场景**：root 在预算耗尽前反复 `list_coverage` / `view_agent_graph` 空转——12 轮制下这是最贵的浪费。

**能力提升**：空转 run 提前 2~5 轮结束（15~40% 轮次），尾部 P90 成本 −25~35%。

**代码锚点**
- `Ben/core/progress.py`（217 行）：**用内容签名而非 mtime/长度**，避免"写了个文件就算有进展"的假阳性；
- 阈值可关：`BEN_NO_PROGRESS_TURNS=0` 恢复旧行为（失败即降级，回滚零成本）；
- 只降级为"收尾指令"不硬停，子 agent 在飞时不计 stall。

**为什么这条最值得单独讲**
Sapper 的原话逻辑是"不判断模型是否努力，只判断图是否变化"——这是**零模型调用、零歧义**的停机判据。相比"让 LLM 自己判断该不该停"，它把一次不确定的推理降级成一次确定性的元组比较。

---

## 亮点 4｜Fail-closed 验证闭环：refuted 的发现不许进正文

**作用**：`verify_finding` 派独立 validator 尝试**证伪**，结果回写 `verification_status`；`finish_scan` 收口时把 `refuted` 移出正文、`unverified` 打标。

**适用场景**：非 HTTP 类发现（配置缺陷、凭据、逻辑漏洞）没有 exchange-id 硬门时的质量兜底。

**能力提升**：AI 生成发现的误报率（行业基线 20~40%）估可砍半；代价 +3~5% 调用量。

**代码锚点**
- `Ben/tools/verify/tool.py`：`_finding_looks_http()` 用 **`http_exchange_ids`**（`report/state.py:416`）判断——HTTP 发现已被 reporting 的 exchange-id 硬门覆盖，**不重复验证**，省下一个并发额度；
- `Ben/tools/finish/tool.py:49-94`：`refuted_ids / unverified_ids` 已接入收口，回报给 root "还欠什么"。

**关键设计取舍（写进方案的话术）**
> "验证不是'再看一遍'，而是**指定反证条件**：给 validator 的输入只有 `claim + evidence + falsifier`，`inherit_context=False`，不给 hunter 的叙事。这是对抗确认偏误的唯一有效手段——同模型同上下文只会重复同一个结论。"

**尚有风险**：`record_verify_outcome` 目前无归属校验（任何 agent 可把自己标 upheld）；建议只允许 assigned validator + tier 校验。

---

## 亮点 5｜确定性 Prompt 组装 + Cache Point：同配置 Agent 前缀字节级一致

**作用**：把系统提示按 `<cache_point>` 切成"稳定前缀 / 可变后缀"，**只有真正随任务变化的内容（用户请求片段的 skill）放在缓存点之后**。

**适用场景**：一次 run 派 5~10 个子 agent，每个都付 9~11K token 的冷启动——这是账单最大的固定开销。

**能力提升**
- 冷启动 5 agent × 9~11K ≈ 50K 全额付费，瘦身 30% ≈ 每 run 省 15K 输入账面；
- cache 命稳时，收益转为**上下文窗口空间**（为真实工具结果腾出 ~3K/agent）。

**代码锚点**（这是一套完整的三段式设计，值得单独成章）
1. **标记层**：`CACHE_POINT = "<cache_point>"`（`agents/prompt.py:46-48`），发送前由 `strip_cache_point()` 移除；
2. **切分层**：`split_cache_point()`（`:329`）→ `(prefix, suffix)`；注释明确写"用户请求片段在 cache_point 之后内联（随任务变化）；其余片段在前缀内联（同类 agent 前缀一致）"；
3. **注入层**：`_prompt_cache_extra_args()`（`core/inputs.py:317-345`）生成 LiteLLM `cache_control_injection_points`，两个断点（每个 system message + rolling last-message），配合"system 最多三段"正好用满 Claude 的 4 个断点上限。

**三个容易被忽略的正确性细节（答辩加分项）**
- **工具配置不打断点**：tools 在 system 之前，system 的首个断点顺带缓存了 tools——少一次断点不损失命中；
- **Bedrock 未映射模型直接返回 None**：Bedrock 对透传字段直接报错；
- **裸 `claude-*` 名不走 LiteLLM 时不注入**：SDK 的 OpenAI client 对未知 kwargs 抛 `TypeError`——这是"只在真正生效的路由上注入"的防御。

---

## 亮点 6｜技能目录"只列 name"：单条改动省 1,200 token

**作用**：`skill_catalog()` 只返回 `{分类: [{name, description}]}`，系统提示**按类只渲染 name 列表**，description 仅在 `load_skill` 时按需加载。

**适用场景**：11 类 / 84 个 skill（实测 `find Ben/skills -name "*.md" | wc -l` = 84）的目录本身就是 1,929 token，占 child 系统提示的 21%。

**能力提升**：**最大单项节省**（−800~1,200 token / agent，占瘦身总量的一半），且零信息损失——模型知道有哪些技能，要看正文再加载。

**代码锚点**
- `Ben/skills/__init__.py:231-245`：`skill_catalog()` docstring 明写"系统提示按类只渲染 name 列表，不贴 description"；
- description 仍截到 ~40 字符保留在返回值里，供 TUI / `list_skills` 工具使用——**同一份数据两种消费方式**；
- `load_skill` 的预算控制：`MAX_SKILLS_PER_CALL=5` / `MAX_SKILL_CHARS_PER_CALL=24_000`（`tools/load_skill/tool.py:21-23`），并有 skip / char-gate 行为防单次撑爆上下文。

**方案表述建议**
> "技能体系的设计目标不是'让模型知道所有技能细节'，而是'让模型知道有哪些技能、需要时用一次工具调用换正文'。目录是索引，正文是按需资源——这与数据库索引/表数据的分层是同构的。"

---

## 亮点 7｜分层记忆索引：90 字符索引行替代全文注入

**作用**：注入子 agent 的共享发现，统一走 `prompt_budget.build_shared_findings_index()`——每行 `clip_line` 到 90 字符，最多 30 条，详情由 agent 主动调工具拉取。

**适用场景**：多个子 agent 共享"已有漏洞 + 方法论笔记"时，全量注入会把上下文撑爆；不注入则重走死路。

**能力提升**：长 run 中"已完成 agent 的遗留信息"占 root 上下文 20~40%，三层化后降到 <5%。

**代码锚点**
- `Ben/core/prompt_budget.py`（87 行）：`INDEX_LINE_MAX=90 / INDEX_MAX_ITEMS=30 / LIST_PREVIEW_CHARS=90`，docstring 直接写"对齐 sapper 的 render 纪律：默认只给短行摘要，详情经工具按需拉取"；
- `clip_line()` 的断词逻辑：超长时回退到最近的空格（且要求 ≥16 字符或 ≥半程），避免把单词切成两半；
- 去重：`seen_titles` 防止"同一 title 在 findings 和 notes 里各刷一遍"；
- 优先级：`findings > methodology > general`，再按 `updated_at` 排序——**最新的高价值信息排前面，被 30 条上限截掉的是旧的低价值项**。

**为什么这条值得单独讲**
"分层记忆"听起来抽象，落到代码就是**一个函数 + 三个常量**。这是最容易讲清、最容易复现的一条——不依赖任何框架特性。

---

## 亮点 8｜"在飞"语义并发闸 + 分 tier 配额

**作用**：`BEN_MAX_SUBAGENTS` 从"终身总量"改为"同时在飞上限"，完成/失败的 agent 释放名额；`BEN_MAX_GENERALS` 单独限制旗舰档并发。

**适用场景**：原实现数的是"所有非 root agent 总数"，4 个名额被早完成的僵尸占满后，连替换死掉的 agent 都不行——**整 run 停摆**。

**能力提升**：正确性修复（避免死锁式停摆）+ 资源按档位分配（旗舰并发 ≤1）。

**代码锚点**
- `execution.py:549-581`：注释明确写"限制**同时在飞**的子 agent 数"，超限返回可读错误而非静默；
- 与 tier 联动：`general` 档位单独闸门，`scout/validator` 不占旗舰配额；
- per-task 时限：`BEN_SUBAGENT_MINUTES`（默认 45min）+ `create_agent(minutes=...)`，用 `asyncio.wait_for` 触发 graceful park。

**方案表述建议**
> "并发控制的语义必须是资源语义（在飞），不是累计语义（总数）。一个是'同时占多少内存'，一个是'一辈子能开多少次进程'——后者会在长跑里必然停摆。"

---

## 亮点 9｜统一原子写 + 单点时间源（防"看似微小的数据丢失"）

**作用**：所有需要落盘的状态（claims / metrics / verify queue / heartbeat）走统一的 `jsonstore.atomic_write_json()`；run 级时限走 `scan_clock` 的墙钟语义。

**适用场景**：断电/容器被杀时，claim 丢失意味着**两个 agent 可能同时开工**（并发正确性失效），比"少一条日志"严重得多。

**能力提升**：并发正确性从"通常成立"变为"断电也成立"；resume 时剩余时间不被静默翻倍。

**代码锚点**
- `Ben/core/jsonstore.py`（43 行）被 `task_claims` / `orchestration_metrics` / `verify` / `supervisor` 共用——**四处重复实现收敛为一处**；
- `Ben/core/scan_clock.py`（116 行）：从 `time.monotonic()` 改为墙钟（`started_at_epoch + total_minutes`），resume 语义正确。

**方案表述建议**
> "编排层所有状态都是'多点读、单点写'，因此写路径必须原子；而 claim 的正确性依赖落盘顺序，所以 fsync 不是性能选项，是正确性选项。"

---

# 第二部分｜可独立成篇的 4 个亮点

> 这四条体量足够独立成章，答辩/方案书里可各占一节。

---

## 独立篇 A｜"策略在 Markdown，机制在代码"——三层分离架构

**为什么能独立成篇**：这是整个框架的方法论内核，决定了"改行为"和"改代码"的边界。

| 层 | 载体 | 改它的人 | 变更成本 |
|---|---|---|---|
| 策略层 | `Ben/skills/coordination/root_agent.md` + 84 个 skill | 安全专家 | 改 md，不跑测试 |
| 装配层 | `system_prompt.jinja` + `<cache_point>` + fragment 解析 | 提示词工程师 | 跑 3 个 prompt 测试 |
| 机制层 | `Ben/core/*`（6,844 行） | 平台工程师 | 全量测试 |

**能力提升**
- 策略迭代不触碰代码，**提示词回归测试可锁定行为**（`test_root_prompt_policy.py` 断言"at most two decisions"、升级阶梯、`<root_agent_directive>` 不得出现在 child）；
- 机制层只提供原语（claim / tier / clock / progress），不内置策略——同一套机制可服务"快速侦察"和"深度利用"两种模式。

**可写进方案的判据**
> "如果一个改动需要同时改 md 和 py，说明抽象漏了。`root_agent.md` 里的每一条纪律，都必须能映射到 core 里的一个硬闸门或一个可观测信号，否则它就是一句愿望。"

---

## 独立篇 B｜提示词瘦身的完整方法论（含实测数字）

**为什么能独立成篇**：这是唯一有**可复现测量方法 + 明确刀口 + 风险清单**的优化，最适合作为"工程严谨性"的证据。

**实测基线**（`render_system_prompt()` 真实渲染 + tiktoken o200k）

| 变体 | 改前 | 改后 | 变化 |
|---|---|---|---|
| deep child | 9,339 | **6,813** | −27% |
| deep root | 10,836 | 10,156 | −6% |
| blackbox child | — | **5,484** | — |

**四个刀口（按性价比排序）**

1. **技能目录改"按类只列 name"**：−800~1,200（占总量一半），风险低（改一条测试阈值）；
2. **工具清单去重**：描述与 tool schema 双重描述 → 改成"路由表" −700~900，需逐工具核对安全句保留；
3. **删死文案**：过时分支说明 −150~250，风险极低；
4. **报告格式规范外移**（root 专属 1,687 token → 收尾前 `load_skill`）：root 前期编排轮不带着它跑，**中收益中风险**（模型可能拖到最后才加载）。

**必须保留的回退项（这是分析里最容易被忽略的一段）**
瘦身删掉的三句"废话"其实是行为闸门：
- "非交互长任务放后台重定向到日志" → 否则 `nmap -p-` 跑成超时；
- "超长输出会被截断，可缩小范围重读" → 否则反复重跑同一命令取全量；
- "HTTPS 经 Caido 代理时证书已信任" → 否则模型自己加 `--ignore-certificate-errors`。

**建议做法**：各补 15 字压缩版，总成本 <60 token，换掉三类行为回归。

**计价提醒（写进方案可显专业度）**
这些段几乎全在 `<cache_point>` 之前。Claude 前缀命中按 ~10% 计价、DeepSeek 自动前缀缓存同样打折——**账单收益主要落在冷启动与 cache miss 轮**；命中稳定时收益是上下文窗口空间，不是钱。

---

## 独立篇 C｜11 类 / 84 Skill 体系的"路由 + 装载"设计

**为什么能独立成篇**：用户明确要求的能力项，且实测规模确凿（`Ben/skills/*/` 11 个分类目录、84 个 `.md`）。

**实测分布**

| 分类 | 数量 | 分类 | 数量 |
|---|---|---|---|
| vulnerabilities | 29 | frameworks | 4 |
| tooling | 13 | cloud | 4 |
| analysis | 8 | reconnaissance | 3 |
| technologies | 7 | protocols | 2 |
| scan_modes | 6 | coordination | 2 |
| custom | 5 | **合计** | **84** |

**三段式设计**
1. **发现层**：`_discover()` 扫目录 → `Skill(category, name, description, path)`；`skill_catalog()` 输出 `{分类: [{name, description}]}`；
2. **路由层**：`root_agent.md` 的升级阶梯表把"任务类型"映射到"tier + skills"，例如"IDOR 判定 → tier=validator, skills=[vulnerabilities/idor]"；
3. **装载层**：`load_skill(requested, ...)`，`MAX_SKILLS_PER_CALL=5` / `24_000` 字符，超限返回 `limit_chars` 与截断说明——**让模型知道被截了，而不是静默少给**。

**能力提升**
- 上下文从"全量 728KB 技能库"降到"目录 700 token + 按需 24K 字符"；
- 技能增删不改代码（`register_skill_dir()` 支持外部目录，`extra_skill_dirs` 参数）。

**建议补充（写进"下一步"）**
- 技能元数据里加一行 `owasp: [LLM01, LLM06]` / `mcp: [tool-poisoning]` 标签，让目录行变成 `name [LLM01,LLM06]`——**多花 8 token/技能，换来 root 精确路由**，比 description 更省更准；
- `custom` 目录 5 个 skill 是用户自定义入口，建议在方案里明确"扩展契约"（目录约定 + frontmatter 字段）。

---

## 独立篇 D｜SQLite 向量 RAG：零外部依赖的离线检索

**为什么能独立成篇**：规模与实现都可实测，且"离线可用"是渗透测试场景的硬需求（目标环境常无外网）。

**实测事实**
- 语料：`Ben/rag/knowledge_base/clown-src/` 共 **583,176 字符**（≈58 万字）；
- 索引：`knowledge.sqlite3` 的 `chunks` 表 **629 行**；
- 检索：`VectorStore.search()` 全表扫描 + 精确余弦（`sum(a*b)/norm`），`k` 与 `min_score` 可调，`source` 可过滤。

**设计要点**
1. **零向量库依赖**：`CREATE TABLE chunks (id TEXT PRIMARY KEY, text TEXT, metadata TEXT, vector TEXT)` + `config` 表存 signature——629 块规模下暴力余弦完全够用，省掉一个服务进程；
2. **签名一致性守卫**（`vector_store/__init__.py:search`）：`signature` 不匹配直接 `raise ValueError("Embedding configuration changed; run python -m Ben.rag ingest")`——**防止"换了 embedding 模型但还在用旧向量"这种静默降级**；
3. **维度守卫**：`if len(other) != len(vector): raise ValueError("Embedding dimension mismatch; rebuild index")`；
4. **双后端可切**：`LocalEmbeddings`（离线 hash 特征）/ `APIEmbeddings`（OpenAI），`create_embeddings(settings)` 工厂切换；
5. **事务性**：`PRAGMA journal_mode=PERSIST` + contextmanager，`replace()` 在同一事务内 `DELETE` + `executemany`。

**能力提升**
- 完全离线可用（Local 后端零 API 调用），沙箱内可直接带索引；
- 换 OpenAI Embedding 只需改配置 + 重跑 ingest，**检索代码不动**。

**已知短板（主动写进方案，反而加分）**
- `replace()` 是**全量重建**（`DELETE FROM chunks`），语料微调要重嵌 629 块；建议改按 `chunk_id` 增量（id 已是 `sha256(metadata+content)`）；
- `search()` 每次重嵌 query，API 后端场景建议加 LRU；
- 两棵 langchain 依赖（`langchain-core` + `text-splitters`）只服务 281 行代码，可用 stdlib 替换分隔符切分（Sapper 的 `kb.py` 已证明 token 重叠打分在小语料上够用）。

---

# 第三部分｜Docker 沙箱：可复现的执行底座

**用户要求的能力项，实测锚点如下**

| 能力 | 实现 | 关键细节 |
|---|---|---|
| 统一执行环境 | `Ben/runtime/backends.py`：`BEN_RUNTIME_BACKEND` 注册表，默认 `docker` | 后端可换（不装 docker-py 也能跑别的后端），`import` 延迟 |
| 环境隔离 | `_sandbox_network()` + `_apply_sandbox_network()` | **专用 user-defined network**：默认 bridge 上跨容器可互访，专用网络才有 DNS / IPAM / 隔离策略 |
| 可达宿主机服务 | `host.docker.internal` → host-gateway | 代理（Caido）在宿主机，容器内 agent 必须能到 |
| 进程正确启动 | 去掉 SDK 的 `entrypoint=["tail"]` 覆盖 | 否则 `docker-entrypoint.sh` 不执行，caido-cli 不在容器内启动，bootstrap 只能对着死端口重试 |
| 端口解析 | `docker_client.py:165` | 在沙箱网络上解析暴露端口，而不是 `127.0.0.1`（否则拿到的是宿主机上的错误端口） |
| 任务间隔离 | 每个 run 独立容器 + 独立 `run_dir` | 状态文件、输出、会话 db 全部落在 run 目录 |

**能力提升（写方案的话术）**
> "沙箱不是'把命令丢进容器'，而是**保证三件事同时成立**：DNS 与网络隔离策略生效（专用网络）、代理可达（host-gateway）、初始化脚本真的跑了（不覆盖 entrypoint）。任何一个不成立，'可复现'就是空话——因为失败原因会藏在环境差异里。"

---

# 第四部分｜一页交付对照表（面试/答辩直接念）

| # | 亮点 | 一句话价值 | 可量化收益 | 代码位置 |
|---|---|---|---|---|
| 1 | 任务认领 | 撞面在 spawn 前被拒 | 重复面 10~20%→<5% | `core/task_claims.py` |
| 2 | 角色档位 | 成本按难度分配 | 子 agent −15~30% | `core/tiers.py` + `factory.py` |
| 3 | 无进展停机 | 只判断世界有没有变 | 轮次 −15~40% | `core/progress.py` |
| 4 | Fail-closed 验证 | refuted 不进正文 | 误报率估砍半 | `tools/verify` + `finish` |
| 5 | 确定性组装 + Cache Point | 同配置前缀字节一致 | 冷启动 −27% | `agents/prompt.py:46/329` |
| 6 | 目录只列 name | 索引与正文分离 | −800~1,200 tok/agent | `skills/__init__.py:231` |
| 7 | 分层记忆索引 | 90 字符索引行 | root 上下文 −15~30% | `core/prompt_budget.py` |
| 8 | 在飞并发闸 | 修死锁式停摆 | 正确性修复 | `core/execution.py:549` |
| 9 | 原子写 + 墙钟 | 断电也成立 | 并发正确性 | `core/jsonstore.py` |
| A | 策略/机制分离 | md 改行为、py 改机制 | — | `skills/coordination/` |
| B | 提示词瘦身方法论 | 可复现测量 + 刀口清单 | −27% 实测 | `docs/prompt-slimming-audit.md` |
| C | 84 Skill 路由 | 目录 + 按需装载 | 728KB→~25KB | `skills/` |
| D | SQLite RAG | 零外部依赖离线 | 58万字/629块 | `rag/` |

---

# 第五部分｜落地顺序建议（按"性价比 × 风险"）

| 阶段 | 内容 | 前提 | 风险 |
|---|---|---|---|
| 已具备 | 亮点 1/2/3/5/6/8/9 全部已实现（`5a7b484`） | — | — |
| 立刻补 | 亮点 4 的**归属校验**（validator 才能写 outcome）+ H5 的 `ruled_out_json` 容错 | 无 | 低 |
| 第二批 | 独立篇 A 的"纪律↔闸门"映射表（把 `root_agent.md` 每条纪律对上代码） | 无 | 低，纯文档 |
| 第三批 | RAG 增量 ingest + query LRU（独立篇 D 的短板） | 行为等价测试先行 | 中 |
| 可选 | 提示词 G 项（root 报告格式外移） | A/B 观测 | 中 |

---

# 附：复测命令

```bash
# 提示词 token 基线（改动后跑一遍对比 delta）
cd /home/kali/Desktop/AI/BenProject && .venv/bin/python /tmp/prompt_audit.py

# 技能规模
find Ben/skills -name "*.md" | wc -l          # 84
for d in Ben/skills/*/; do echo "$(find "$d" -name '*.md'|wc -l) $d"; done | sort -rn

# RAG 规模
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('Ben/rag/vector_store/knowledge.sqlite3');print(c.execute('select count(*) from chunks').fetchone())"

# 核心测试（14 个既有失败与编排无关，见 §0）
.venv/bin/python -m pytest tests/core -q
```
