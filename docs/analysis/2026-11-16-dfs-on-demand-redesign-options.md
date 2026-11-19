# Ben 项目架构审查与"深度优先按需加载"改造方案对比

日期：2026-11-16
范围：`Ben/agents/factory.py`、`Ben/core/`（runner / execution / frontier / task_claims / agents）、`Ben/tools/`（todo / coverage / proxy / mcp / output_store）

---

## 一、现状审查结论

### 1.1 代理初始化与工具注册

- 唯一构建入口是 `Ben/agents/factory.py` 的 `build_agent()`（1182-1292）与 `make_child_factory()`（1298-1364）；root 在 `core/runner.py:975-990` 构建，子 agent 经 `core/execution.py:499-712` 的 `spawn_child_agent()` 派生。
- **工具为全量预加载**：静态清单 `_BASE_TOOLS`（factory.py:1021-1067）共 43 个 function tool（todo×6、notes×5、coverage×3、threat_model×3、reporting×6、proxy/Caido×6、MCP 发现派发×5、图编排×5、web×2、think/load_skill/search_knowledge），外加 shell capability（`exec_command`/`write_stdin`）与 filesystem capability（`view_image`/`apply_patch`）。
- **裁剪只按角色**（root vs child）：`_ROOT_NETWORK_DROP` / `_ROOT_REPORT_WRITE_DROP`（factory.py:1071-1097）。`scan_mode` 只影响提示词（`agents/prompt.py:67-113` 选 skill 片段），**不影响工具集**。
- **没有专用的端口扫描/目录枚举 agent 工具**。沙箱 shell 白名单（factory.py:401-407 `_NET_CLIENT_RE`）放行 `nmap/masscan/ffuf/gobuster/nuclei` 等二进制，由模型经 `exec_command` 直接调用；另有不经 agent 的确定性侦察 CLI：`interface/cli/scout.py`（nmap 端口扫描）与 `interface/cli/recon.py`（目录/路径探测）。

### 1.2 任务队列与调度

- **代码级不存在任务队列**：全仓无 heapq / PriorityQueue / deque 调度器。所谓"价值插队"实际是 **LLM 驱动的软调度**：
  - todo 台账（`tools/todo/tools.py`）按 `(status, priority, created_at)` 排序展示，**排序只影响展示，模型自己挑活**；
  - frontier 块（`core/frontier.py:226` `render_frontier_block`）每轮向 root 注入 ≤6KB 的 Ready（unclaimed needs_follow_up）/ Claimed 状态，并按 `_RISK_SUGGEST` 表给静态建议；
  - 认领去重 `core/task_claims.py`（键 = `(surface, risk_area)`，TTL 67.5 分钟，`BEN_CLAIM=1` 才启用）；
  - spawn 闸门（并发上限 `BEN_MAX_SUBAGENTS=4`、general 档上限、残局禁新面）——**超限时拒绝而非排队**。
- 层级关系仅存在于 **agent 树**（`AgentCoordinator.parent_of`）；todo/coverage 均为扁平结构。目标侧的 URL 树在 **Caido sitemap**（`list_sitemap(parent_id=, depth=)`）里，Ben 进程内没有自建 URL 树。
- "Sapper" 全为注释/文档级设计渊源（frontier.py、runner.py、scout.py 的 fact 导出），无代码依赖。

### 1.3 扫描状态管理与去重

- "已扫面"去重的承担者是 **coverage 账本**（`tools/coverage/tools.py:34`）：模块级 `_coverage_storage` + RLock，键为 `(surface.lower(), risk_area.lower())`，重复记录被拒绝并返回已有 entry（`_duplicate_of_locked`，171-188）；内存 + 磁盘双写（`strix_runs/<run>/.state/coverage.json`，原子 rename），resume 时统一 hydrate（`core/runner.py:773-786`）。
- **没有 URL/路径粒度的全局已扫哈希表**。Caido sitemap 是客观上最接近"已访问路径"的记录，但查询发生在 Caido 侧，agent 不主动查就不进入决策。
- 另有内容签名机制：`core/progress.py` 对世界状态做 SHA-256 签名检测"无进展"；`tools/output_store.py` 按内容寻址溢写大输出。

### 1.4 响应解析

- 原始 HTTP body **会整块进入上下文**，三条入口各有上限：`repeat_request` 8KB（`proxy/caido_api.py:217-255`）、`web_get_contents` 20KB/页（`tools/web_search/tool.py:64-94`）、`exec_command`（curl 整页 HTML，受 2000 行 / 50KB / 8000 token 三重帽）。
- 已有部分节省机制：`list_requests` 只回元数据；`view_request` 带 `search_pattern` 时只回 ≤20 个命中片段；所有工具输出统一过 `_with_bounded_result` → `output_store.bound_and_store`（头尾截断 + 溢写沙箱文件供 grep 读回）；会话级 `maybe_compact` 用 flash 模型做检查点压缩。
- **但没有"只提取必要字段"的结构化解析**——截断是"限量"，不是"提炼"。

### 1.5 对改造前提的两点修正

1. 现状**没有"价值插队调度"的代码结构**可供"替换"——真实改动是**新增**代码级树形 DFS 队列，把调度权从 LLM 自由挑选收回到代码层。
2. "原全局哈希表"实为 `(surface, risk_area)` 粒度的 coverage 账本，不是 URL 级；URL 级去重需要新建或扩展。

---

## 二、需求 1：初始阶段仅保留端口扫描 + 目录枚举，关闭全量预加载

### 方案 A1：静态分阶段工具面（stage profile）

- **核心思路**：给 `build_agent` 增加 `profile` 参数（如 `recon` / `exploit` / `validate`），`_BASE_TOOLS` 拆分为 profile 集合。`recon` 档只挂 `exec_command`（白名单收窄到 `nmap/naabu/ffuf/gobuster`）+ `record_coverage` + `finish`。阶段推进由队列节点类型决定，子 agent 按节点 profile 构建。
- **适用场景**：工具 schema 占用是主要静态 token 成本（43 个 schema 每轮都在），且阶段边界清晰的项目。
- **优点**：token 节省最大（schema 直接不进上下文）；实现直观，沿用现有 `_ROOT_NETWORK_DROP` 的裁剪模式；recon agent 工具面小、误用率低。
- **缺点**：`openai-agents` 的 `Agent.tools` 构建后不可热更新，跨阶段需要重建 agent 或换子 agent（对 Ben 恰好自然——子 agent 本来就按任务派生）；profile 划分需要维护。
- **DFS 按需加载实现**：DFS 队列 pop 出节点 → 按节点类型（host/port/dir/endpoint）映射 profile → 以该 profile spawn 子 agent → 该节点只加载自己需要的工具。

### 方案 A2：运行时工具门控（wrapper 阶段锁）

- **核心思路**：工具仍全量注册，但在统一包装链（`_with_bounded_result` 旁）加一层 `_with_stage_gate`：recon 阶段调用非白名单工具直接返回"当前阶段不可用"提示；阶段翻转由调度器解锁。
- **适用场景**：希望单 agent 全程存活、不重建 agent；或先做低风险试点。
- **优点**：侵入最小（只加一层 wrapper + 一个全局阶段标志）；可动态翻转，支持阶段回退。
- **缺点**：**不省 schema token**（43 个工具描述仍在每轮 prompt 里），只省了误调用；模型可能被"不可用"反馈反复试探，浪费轮次。
- **DFS 按需加载实现**：门控的"当前阶段"由 DFS 队列当前节点驱动——pop 节点时设置 stage，工具可用性跟随节点类型切换。

### 方案 A3：元工具按需派发（类 MCP discovery 模式）

- **核心思路**：仿照现有 MCP 五件套（`list_mcps/search_mcp_tools/call_mcp`），agent 只挂 `exec_command` + `list_tool_profiles` + `activate_profile` 等少数元工具，工具组按 profile 打包，激活后才进入可用面。
- **适用场景**：工具族会持续增长、希望工具面完全数据驱动的长期形态。
- **优点**：静态 schema 极小；扩展新工具族零改 agent 构建代码；与 MCP 现有模式同构，心智一致。
- **缺点**：增加一层间接（模型要先发现再激活，多花 1-2 轮）；openai-agents 下"激活"仍需重建/换 agent 或靠门控实现，本质上是 A1/A2 的组合封装；调试链路变长。
- **DFS 按需加载实现**：队列节点自带所需 profile 元数据，子 agent 首消息指示其激活对应 profile，顺序由 DFS 栈保证。

---

## 三、需求 2：树形深度优先队列（替代 LLM 自由调度）

### 方案 B1：代码级 DFS 栈队列（新模块 `core/task_tree.py`）

- **核心思路**：新建任务树模块：节点 = `(node_type, host, port, path, parent_id, status)`，根为 target，端口结果生成 port 子节点，目录枚举生成 dir 子节点。维护 LIFO 栈，每次 pop 最深节点生成子 agent 任务；结果回填时把新发现节点 push 到当前节点之下（标准 DFS pre-order）。`frontier.py` 改为渲染"队列下一批节点"而非 Ready 列表。
- **适用场景**：要求扫描顺序可预测、可复现、可审计（对标 Sapper 的树层级领取）；希望削减 root 编排轮次。
- **优点**：调度确定性最强，token 最省（root 不再每轮从 Ready 列表做选择题）；与 claims/coverage 键可复用；天然支持断点续扫（栈持久化）。
- **缺点**：新增核心模块 + 改 spawn/回收/回填链路，侵入最大；树爆炸需要深度/宽度闸门；LLM 失去自由度后，队列质量决定扫描质量。
- **DFS 按需加载实现**：本方案即 DFS 本体——栈序即加载序，pop 时联动 A1 的 profile 装配工具。

### 方案 B2：frontier 渲染改造为 DFS 序（提示词层调度）

- **核心思路**：不建队列数据结构，只把 coverage/sitemap 数据在渲染 frontier 块时按"父面未完的子面优先"排成 DFS 序列表，root 照旧从列表领任务。
- **适用场景**：快速验证 DFS 顺序对覆盖率/重复率的收益，不想动调度链路。
- **优点**：侵入最小（只改 `render_frontier_block` 的排序逻辑）；保留 LLM 灵活性（可跳过无价值节点）。
- **缺点**：顺序只是"建议"，模型可不遵守，确定性弱；每轮仍要渲染列表，token 节省有限。
- **DFS 按需加载实现**：渲染顺序即 DFS 序，模型按列表第一项领任务近似深度优先；工具面不变（需搭配 A1/A2 才省 schema）。

### 方案 B3：复用 Caido sitemap 树 + DFS walker

- **核心思路**：不自建树，直接以 `list_sitemap(parent_id, depth)` 的现成 URL 树为数据源，写一个 walker 按 DFS 顺序产出待扫节点。
- **适用场景**：流量主要经代理产生（浏览器/重放驱动）的扫描；希望攻击面数据单一事实源。
- **缺点**：sitemap 只记录"代理见过的流量"，主动枚举（ffuf 爆出来的路径）不会自动进树，需回写适配；依赖 Caido 可用性；节点语义（目录聚合）与扫描任务粒度不完全对齐。
- **优点**：零新数据结构；URL 树客观真实。
- **DFS 按需加载实现**：walker 的 DFS 游标持久化在 `.state/`，每次出队一个 sitemap 节点驱动一次 spawn。

---

## 四、需求 3：保留全局哈希表做已扫路径去重

### 方案 C1：新增 URL 规范化哈希集合（独立模块）

- **核心思路**：新建 `core/visited.py`：`normalize_url()`（小写 scheme/host、去默认端口、路径去尾斜杠、query 参数排序、去追踪参数）→ SHA-1/xxhash → 全局 `set[str]`，内存 + `.state/visited.json` 双写；DFS 队列 push 前查重，重复直接跳过。
- **优点**：语义最清晰，与 B1 队列解耦；O(1) 查询；规范化规则可单测。
- **缺点**：又一套状态账本（与 coverage/claims 并列），hydrate/persist 样板代码重复；URL 规范化的边界 case（大小写、编码、参数语义）需要持续维护。
- **适用场景**：希望"路径已扫"与"面覆盖结论"两种语义分开管理。

### 方案 C2：扩展 coverage 账本（复用现有设施）

- **核心思路**：在 coverage entry 增加 `path_hash` 字段，`_duplicate_of_locked` 增加一路按 hash 的查重；规范化函数放 `utils/`。push/spawn 前调 `coverage_has(path_hash)`。
- **优点**：**复用现有锁、原子持久化、hydrate、resume 全套设施，侵入最小**；去重结果自动出现在 `list_coverage`/frontier，可观测性现成。
- **缺点**：coverage 语义被拓宽（从"面结论"混入"路径到访"），账本体积膨胀；`(surface, risk_area)` 与 URL 双键并存增加概念负担。
- **适用场景**：追求最小改动、接受语义轻度混合。

### 方案 C3：以 Caido sitemap 为唯一事实源 + 本地缓存

- **核心思路**：不建 Ben 侧哈希表，去重 = 查 sitemap 是否已有该 URL；启动时全量拉取建内存缓存，随后增量刷新。
- **优点**：零新账本；去重基于客观流量而非 agent 自报。
- **缺点**：ffuf 枚举结果需先回写 Caido 才能去重（回写链路不存在，要新建）；对 Caido 依赖加重；缓存失效策略复杂。
- **适用场景**：代理流量为主的扫描形态。

---

## 五、需求 4：响应解析精简为必要字段

### 方案 D1：结构化字段提取器（proxy/工具层）

- **核心思路**：在 `repeat_request` / `view_request` 返回前加解析层，只提取：`status_code`、关键 headers（content-type/location/set-cookie/server）、`title`、表单（action/method/inputs 名）、链接与脚本 src 清单（去重后 top-N）、body 长度与 hash、前 200 字符摘要。原始 body 一律溢写沙箱文件。
- **优点**：token 节省最直接（8KB→通常 <500 字符）；确定性解析，无 LLM 成本；字段即侦察事实，可直接喂给 DFS 队列（链接 → 新节点）。
- **缺点**：提取器要覆盖 HTML/JS/JSON 多类型，有开发量；提取规则漏掉关键内容时模型需二次取全文。
- **适用场景**：页面量大、重复结构多的 Web 目标。

### 方案 D2：双档返回（summary 默认 + expand 参数）

- **核心思路**：所有返回 body 的工具加 `detail: "summary"|"full"` 参数，默认 summary（D1 的字段集）；模型确需原文时显式 `detail="full"`（仍受既有上限与溢写约束）。推广 `view_request` 现有 `search_pattern` 的"按需取片段"哲学到全部入口。
- **优点**：侵入小（逐工具加参数 + 复用 D1 提取器）；保留逃生舱，不牺牲能力；token 节省靠默认值达成，模型行为改动小。
- **缺点**：依赖模型不滥用 full 档；summary 质量仍取决于 D1 提取器。
- **适用场景**：与 D1 组合落地，是 D1 的交付形态。

### 方案 D3：统一出站解析管道（output_store 前置 parser）

- **核心思路**：在 `_with_bounded_result` 之前插入内容类型识别 + 解析器注册表（HTML→字段提取、JS→端点提取、JSON→键摘要），所有工具输出统一过管道。
- **优点**：一处接入、全工具受益（含 curl 的 stdout）；架构最干净。
- **缺点**：在通用包装层做内容语义解析，层次混乱（output_store 是通用设施）；curl 输出含请求命令的任意 stdout，误判风险高；改动面大、回归风险高。
- **适用场景**：长期重构方向，不建议作为本次落地点。

---

## 六、横向对比总表

| 方案 | 实现复杂度 | token 消耗（相对现状） | 扩展性 | 代码侵入性 |
|---|---|---|---|---|
| A1 静态 profile | 中 | **大幅降低**（recon 期省 ~40 个 schema/轮） | 中（profile 需维护映射） | 中（factory + spawn 链路） |
| A2 运行时门控 | 低 | 略降（省误调用，不省 schema） | 中 | **低**（一层 wrapper） |
| A3 元工具派发 | 高 | 大幅降低（schema 最小化） | **高**（工具族数据驱动） | 中高 |
| B1 DFS 栈队列 | 高 | **大幅降低**（省 root 编排轮次） | 高（树节点类型可扩展） | **高**（新核心模块 + 回填链路） |
| B2 frontier DFS 序 | 低 | 略降 | 低 | **低**（改排序函数） |
| B3 sitemap walker | 中 | 中 | 中（受 Caido 约束） | 中 |
| C1 独立 visited 模块 | 中 | 中（省重复扫描轮次） | 高 | 中（新账本） |
| C2 扩展 coverage | **低** | 中 | 中 | **低**（复用全套设施） |
| C3 sitemap 事实源 | 高 | 中 | 低 | 高（需回写链路） |
| D1 字段提取器 | 中 | **大幅降低**（8KB→<500 字符/次） | 中（解析器按类型扩展） | 中 |
| D2 双档返回 | 低 | **大幅降低**（默认值驱动） | 高（全工具通用模式） | **低** |
| D3 统一解析管道 | 高 | 大幅降低 | 中 | **高**（通用层混入语义） |

---

## 七、推荐方案

**推荐组合：A1 + B1 + C2 + (D1+D2)**

- **B1（DFS 栈队列）是骨干**：它把"价值插队"的 LLM 软调度收编为代码级确定性调度，DFS 栈序天然就是"按需加载的顺序"——pop 出节点才知道它是什么类型，才知道需要什么工具。
- **A1（profile 工具面）挂在 B1 上**：队列节点类型 → profile 映射，recon 节点只给 `exec_command`（收窄白名单到 nmap/ffuf）+ coverage 工具；exploit 节点才挂全量。子 agent 本来就按任务派生，profile 化不需要"热换工具"这种 SDK 不支持的操作。若需快速试点，可先用 A2 门控过渡，但应以 A1 为终态（schema token 才是大头）。
- **C2（扩展 coverage 加 path_hash）**：复用现有锁/原子写/hydrate/resume 全套设施，是满足"保留原全局哈希表去重"语义的最小侵入路径；队列 push 前查 `path_hash`，重复路径直接丢弃，避免重复扫描浪费 token。
- **D2 交付、D1 实现**：所有 body 入口默认 summary 档（status/关键 headers/title/表单/链接/长度 hash），`detail="full"` 作逃生舱；提取出的链接清单同时回喂 B1 队列作为新节点，形成"解析 → 去重 → 入队 → DFS 领取"的闭环。
- **落地顺序建议**：D2+D1（收益最快、风险最低）→ C2（去重先行，队列依赖它）→ B1（核心调度）→ A1（profile 化收尾）。B2 可作为 B1 上线前的灰度验证（只改 frontier 排序观察效果）。
