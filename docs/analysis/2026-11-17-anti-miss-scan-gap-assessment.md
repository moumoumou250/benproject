# Ben 防漏扫能力评估：五项要求逐项结论与优化方案

日期：2026-11-17
评估对象：当前工作区实现（`Ben/core/task_graph.py`、`Ben/agents/profiles.py`、`Ben/tools/coverage/`、`Ben/tools/finish/`、`Ben/interface/cli/{scout,recon,seed}.py`、`Ben/report/`）

---

## 0. 现有防漏扫机制盘点（评估基准）

| # | 机制 | 位置 | 作用 |
|---|---|---|---|
| M1 | coverage 账本 + 5 类 outcome + 证据强制 | `Ben/tools/coverage/tools.py:34,40-48,171-188` | 面级"测了什么、怎么收尾"，`(surface,risk_area)` 去重，`ruled_out/not_applicable/needs_follow_up` 必须带 evidence |
| M2 | fact/intent 图 + 身份哈希去重 | `Ben/core/task_graph.py:54,166-177,382-395` | 发现物去重（SHA-1 of kind+canonical identity）、DFS pick、claim/conclude/reopen |
| M3 | 认领租约 | `Ben/core/task_claims.py:24,107-181` | surface×risk_area 同时只有一个 owner，TTL 67.5 min |
| M4 | 技能覆盖差集 | `Ben/report/coverage.py:230-268` | "派了该技能但无 coverage 行" → unexamined 警告（仅警告，不补扫） |
| M5 | 无进展签名 | `Ben/core/progress.py:74-217` | 世界状态 SHA-256 连续 3 轮不变 → 收尾指令 |
| M6 | 残局闸门 | `Ben/core/scan_clock.py`、`execution.py:528-547` | 剩余 <20% 墙钟禁新 surface |
| M7 | 卡死检测 | `Ben/core/supervisor.py:121,189`、`execution.py:1426` | 心跳停滞 → park_waiting("stalled")，不重排 |
| M8 | 对抗性验证 | `Ben/tools/verify/tool.py:126-276` | root-only `verify_finding` 派生 validator 子 agent 做证伪 |
| M9 | 结构化出口契约 | `Ben/agents/profiles.py:63-174`、`agents_graph/tools.py:866-950` | recon/enum 必须先落账再 `agent_finish`，否则拒绝 |
| M10 | 确定性侦察 | `Ben/interface/cli/scout.py`（nmap -sT/-sV、banner、路径表）、`recon.py:63-243` | 端口/服务/默认路径探测，不经 LLM |

---

## 1. 逐项评估结论表

| # | 要求 | 结论 | 一句话依据 |
|---|---|---|---|
| 1 | 全量基准清单（子域名/端口/目录/专项路径 + 服务类型→专属清单） | **部分满足** | 端口扫描与服务指纹强（scout nmap -sV + banner + `_service_role`）；目录仅有静态路径表 `_DEFAULT_PATHS/_SCOUT_PATHS`；**子域名枚举缺失**（subfinder 仅存在于 skill 知识，scout CLI 无此分支）；**基准清单未物化成对象**（扫到才算 fact）；**服务类型不驱动检测清单**（28 个 vuln skill 靠 LLM 自选，无映射表） |
| 2 | 唯一标识 + 立即写入 + 失败/卡死进重试队列 | **部分满足** | 哈希表存在且即时落盘（M2：`_dedupe_index` + `atomic_write_json`）；但覆盖对象只有 fact，coverage 用 uuid、baseline 不存在；**无 attempts 计数、无 retry 状态、无重试队列**——`reopen_intent` 只挂 spawn 失败路径（`execution.py:749`），stalled 仅 park（M7）不重排，超时/崩溃的 claimed intent 永久泄漏 |
| 3 | 重试队列优先重跑 + 基准 vs 哈希差集 + 定向补扫 | **不满足** | 无重试队列可跑；无 baseline 可做差集；最接近的 M4 是"技能声明 vs coverage 行"差集，且只产出警告文案（`finish/tool.py:164-169`），不生成补扫任务 |
| 4 | 同目录关联项绑定检测组，前项无论成败触发后项 | **不满足** | 最接近的 `batch_sibling_intents`（`task_graph.py:534-557`）是"同父同 profile ≤5 项合并进一个任务"的**并行合批**，不是组内串行联动；intent 依赖语义是 `from` fact 必须存在（`_is_ready_locked:376-379`），即**成功才解锁，失败永不解锁** |
| 5 | 漏报风险评分（加密/弱回显/编码加权）+ 按分排序 + 人工复核 | **不满足** | 全仓无评分实现；`reporting/tool.py:148-154` 的 severity 是已确认漏洞的 CVSS，不是"漏报风险"；无加密/弱回显/编码特征识别；无 review queue 输出 |

---

## 2. 需求 1：全量基准清单（部分满足）

### 2.1 缺口定位

- 子域名：`scout.py` 只接受单一 `target` URL（1699-1715 参数区），无 subfinder/amass/dnsx 调用；`skills/reconnaissance/asset_discovery.md:59` 只教模型自己跑。
- 基准清单：scout 产出 `ReconResult`（端口/服务/WAF/路径探测结果），recon 产出 `Response` 列表，都是**探测结果**而非"应扫全集"；没有对象承载"目标应有多少项、已完成多少项"。
- 服务→专属清单：`_service_role()`（scout.py:499）只做角色分类（web/db/…），不产出"该服务该查哪些漏洞/路径"。

### 2.2 优化方案

**新增模块 `Ben/core/baseline.py`（约 200 行）**

```python
BaselineItem = {
    "id": "b" + sha1(f"{kind}|{target}|{extra}")[:10],   # 与 task_graph 同款身份哈希
    "kind": "subdomain|port|dir|vuln_path|service_check",
    "target": "10.0.2.15:8080/manager/html",
    "parent": "b3f2a1c0d9" | None,
    "source": "scout|recon|seed|enum|derived",
    "service": {"product": "tomcat", "version": "9.0.45", "role": "web"} | None,
    "checks": ["insecure_deserialization", "weak_password_detection"],  # 应有检测项
    "status": "pending|running|done|failed|skipped",
    "group_id": None,
}
```

- 存储：`{state_dir}/baseline.json`，与 coverage/task_graph 完全同构（RLock + `atomic_write_json` + `hydrate_baseline_from_disk`），hydrate 挂在 `Ben/core/runner.py:773-786` 链尾。
- 构建入口：`build_baseline(seed_facts, scout_result, recon_result)`，在 runner hydrate 之后、首轮 frontier 之前调用；`enum` 阶段每发现新路径经 `ingest_discovery()` 增量并入并去重（复用 `canonical_url`）。
- 子域名：`Ben/interface/cli/scout.py` 增 `--domain` 分支 + `_SUBDOMAIN_SCRIPT`（`subfinder -d {domain} -all -silent -json` → `host` fact + `subdomain` baseline item），`enumerate_subdomains()` 与现有 `scan_script()` 同级；`scout` 的 shell 白名单（`profiles.py:94-108` recon 档）补 `subfinder/dnsx/httpx`。
- 服务→清单映射：新增**纯数据表** `Ben/skills/catalog/service_checks.json`（评审友好、加服务只改表）：

```json
{
  "tomcat": {"paths": ["/manager/html", "/host-manager/html"],
             "checks": ["insecure_deserialization", "weak_password_detection"]},
  "nginx":  {"paths": ["/nginx_status", "/..%2f..%2fetc/passwd"],
             "checks": ["path_traversal_lfi_rfi", "http_request_smuggling"]},
  "jenkins": {"paths": ["/script", "/manage"],
              "checks": ["rce", "broken_function_level_authorization"]}
}
```

- 消费点：`task_graph._derive_intent_locked()`（task_graph.py:245-278）在 `kind == "service"` 时查表，为每个 check 派生 `service_check` intent（profile 按 `validate`/`exploit`），并把 `checks` 写入对应 baseline item——这就是"先识别服务类型，再生成专属清单"的落点。
- 与 M1 的分工：baseline 管"应该扫什么"（期望集合），coverage 管"扫到了什么结论"，task_graph 管"下一步干什么"。三者键不同、互不替代。

**验收**：带 `--seed` 起跑，`baseline.json` 条目数 = 子域名 + 开放端口 + 路径表命中 + 服务 check 展开数；tomcat 服务的目标必含 `/manager/html` 与 2 个专项 check。

---

## 3. 需求 2：唯一标识与进度跟踪（部分满足）

### 3.1 缺口定位

- 哈希标识：只覆盖 fact（`dedupe_key`），baseline item 无标识，coverage 用 `uuid` 且无哈希列。
- 即时写入：fact/coverage 都即时原子落盘 ✓；但"检测子项完成"没有机器侧记录——coverage 是 agent 自报，agent 崩溃则该项无任何痕迹。
- 失败/卡死：无 `attempts`、无 `retry` 状态、无队列；`reopen_intent` 仅 spawn 失败；stalled 只 park；claimed intent 无 TTL（M3 的 TTL 只管 claims，不管 intent）。

### 3.2 优化方案

**① 统一标识**：baseline item id 直接复用 `task_graph.dedupe_key()` 的哈希算法；新增 `baseline_id` 字段同时写进 intent 与 coverage entry（`coverage/tools.py` 的 entry schema 加一列），三账本用同一把钥匙对齐。

**② 机器侧进度写入**：新增 `Ben/core/scan_ledger.py`（约 80 行），或在 `task_graph` 内加 `mark_item_status(intent_id, status, reason)`：

- 写入点：spawn 成功（→running，记 `started_at`）、`agent_finish`（→done）、终态钩子（→failed）、reaper（→failed/reopened）
- 每次状态变更即 `_persist_locked()`，与 fact 同一次原子写

**③ 重试队列**：新增 `Ben/core/retry_queue.py`（约 120 行）

```python
RetryItem = {
    "intent_id": "i012", "baseline_id": "b3f2a1c0d9",
    "reason": "spawn_failed|timeout|crashed|stalled|no_structured_exit|tool_error",
    "attempts": 1, "max_attempts": 2,
    "last_error": "...", "created_at": 1726...,
}
```

- 存储 `{state_dir}/retry_queue.json`；API：`enqueue()` / `drain(limit)` / `size()`
- 触发点三处：
  1. `Ben/core/execution.py:1668-1680`（child 退出处）：agent 的 `metadata.intent_id` 存在且未 done → `enqueue()` + `conclude_intent(status="abandoned")`——**这条同时堵住 claimed 泄漏（上一轮评审的 P0）**
  2. `execution.py:749`（spawn 失败）：改为入队而非静默 reopen
  3. `Ben/core/supervisor.py:189`（stalled）：入队并标记 abort 原因
- 退避：确定性退避（`min(2**attempts, 30)` 秒），不做随机，保证复跑顺序可复现

**验收**：kill 一个 recon child → retry_queue 出现该 intent 且 attempts=1；resume 后该 intent 重新可领；同一 intent 第 3 次失败后不再入队（转 `needs_follow_up` coverage 行）。

---

## 4. 需求 3：失败重试与差集比对（不满足）

### 4.1 缺口定位

- 无重试队列可"优先重跑"（依赖 §3.2-③）。
- 无 baseline，因此无"基准 vs 哈希"差集；M4 的 `skill_coverage_gaps` 只是"技能声明 vs coverage 行"的弱差集，且仅输出 warning 文案（`finish/tool.py:164-169`）。
- 无补扫任务生成：`finish_scan` 收尾只做汇总，不回头补扫。

### 4.2 优化方案

**新增 `Ben/core/reconcile.py`（约 150 行）**

```python
def reconcile() -> dict:
    baseline = load_baseline()
    done_ids = {i["baseline_id"] for i in intents if i["status"] == "done"}
    fact_ids = {fact["baseline_id"] for fact in facts if fact.get("baseline_id")}
    missing = [b for b in baseline if b["id"] not in done_ids | fact_ids]
    return {"missing": missing, "by_kind": Counter(b["kind"] for b in missing),
            "retry_pending": retry_queue.size()}
```

**收尾顺序改造（`Ben/tools/finish/tool.py:103` 之前插入）**：

```
① drain_retry(limit=BEN_RETRY_DRAIN_MAX 默认 10)   # 先跑重试队列，优先级高于一切新任务
② missing = reconcile()["missing"][:BEN_RECONCILE_MAX_ITEMS]   # 默认 20
③ 为每个 missing 生成补扫 intent（kind→profile 映射表），深度 = 其父项深度 + 1（复用 DFS 排序）
④ 受 M6 残局闸门与 BEN_MAX_SUBAGENTS 约束；墙钟不足时降级为 needs_follow_up coverage 行
⑤ 产出 {run_dir}/baseline_diff.json + 在报告中新增 "Unscanned baseline items" 段
```

- 报告落点：`Ben/report/writer.py` 增加一节（复用 `report/coverage.py` 的 gaps 渲染风格），列出缺失项及原因（未发现 / 超时 / 被闸门拒绝）。
- 与 M4 的关系：M4 保留为"风险类"维度，reconcile 补上"资产项"维度，两者在 finish 汇总里并列。
- 可选 CLI：`ben baseline diff --run <dir>` 供离线复核。

**验收**：注入一个不被扫描的 dir item → finish_scan 返回的 diff 包含它；补扫后该 item 状态变 done；墙钟不足时它出现在 `needs_follow_up` 而非静默消失。

---

## 5. 需求 4：关联检测分组联动（不满足）

### 5.1 缺口定位

- `batch_sibling_intents`（task_graph.py:534-557）：同 `from` fact 集合 + 同 profile 的 open intent 合并进**同一个任务**（≤5），是"并行合批"。
- 解锁语义：`_is_ready_locked`（376-379）要求 `from` 中 fact **全部存在**——只有前序成功才会派生 fact，失败则后继永不 ready。
- 无 group/chain 结构，无 `unlock_policy`。

### 5.2 优化方案

**扩展 `Ben/core/task_graph.py`（约 +120 行）**

```python
# 新增顶层结构
groups: dict[str, {
    "id": "g007",
    "from": ["f012"],            # 组所依附的 fact（目录/服务）
    "profile": "enum",
    "items": ["i021", "i022", "i023"],   # 有序检测项
    "cursor": 0,
    "mode": "chain",             # chain = 组内串行；batch = 并行合批（保留现状）
}]

# Intent 增字段
"group_id": "g007",
"unlock_policy": "always" | "require_fact",   # 默认 require_fact
```

- `add_group(profile, from_facts, titles, mode="chain")`：同目录/同服务的关联项建组；组内顺序由固定出站规则决定（发现 → 枚举 → 探测 → 专项），不依赖创建顺序。
- `_is_ready_locked` 支持 `always`：只检查组 cursor 是否指向自己（**前项 done / abandoned / failed 都推进 cursor**）。
- `conclude_intent`（task_graph.py:435-451）末尾：`cursor += 1` → 下一项 `status="open"` → 持久化。**这是"无论成败都触发后一项"的唯一落点。**
- `batch_sibling_intents` 改为组语义：`mode="batch"` 保持现状，`mode="chain"` 走 cursor；recon/enum profile 的 `batch_ok` 改为 `chain_ok`（profiles.py）。
- 与 DFS 兼容：组视为一个复合 intent，深度按首项计算，`pick()` 无需改动；组内串行天然阻止"同目录项被并发打散"。
- 触发点：`conclude_intent` + reaper（§3.2-③）+ `agent_finish`（agents_graph/tools.py:958）。

**验收**：建 3 项链组 → 第 1 项失败 → 第 2 项仍 open 可领 → 第 3 项在第 2 项结束后解锁；组内并发度恒为 1。

---

## 6. 需求 5：漏报风险评分与人工复核（不满足）

### 6.1 缺口定位

- 无评分模块；severity 是 CVSS（已确认漏洞的严重度），语义不同。
- 无加密/弱回显/编码特征识别：`response_summary` 虽已落 status/长度/hash（设计文档 §5），但未用于风险判断。
- 无 review queue 产物，报告无人工复核段。

### 6.2 优化方案

**新增 `Ben/core/miss_risk.py`（约 160 行）**

```python
WEIGHTS = {                       # 外部化为 Ben/config/miss_risk_weights.json
    "tls_only": 0.30,             # 全站 HTTPS/自签/证书异常，回显被加密层遮蔽
    "custom_protocol": 0.25,      # 非 HTTP 服务（gRPC/JDWP/Redis 协议）
    "weak_echo": 0.25,            # 响应与基线同长/同 hash、200 空 body、统一错误页
    "encoded_body": 0.20,         # gzip/br/chunked 嵌套 base64/unicode 混淆
    "auth_wall": 0.20,            # 401/403/302→登录，未验证即收尾
    "no_banner": 0.15,            # 无 banner、无 version、服务指纹 unknown
    "timeout": 0.15,              # 探测超时/连接重置
    "unknown_service": 0.10,
    "nonstandard_port": 0.10,
}

def score(item, probe, coverage) -> tuple[float, list[str]]:  # → (0~1, reason codes)
```

- 特征来源：`response_summary` 的 `status/body_len/body_sha1/content-type/headers`（设计 §5 已定）、scout 的 `_fingerprint/_waf_signal`、coverage outcome（`ruled_out` 但 evidence <阈值 → 加 0.1）。
- 触发：`finish_scan` 收尾，在 §4 的 reconcile 之后对所有 `pending/failed/弱证据` 项计算。
- 输出 `{run_dir}/review_queue.json`：

```json
[{"baseline_id": "b3f2", "target": "https://x/login", "score": 0.72,
  "reasons": ["weak_echo", "tls_only", "auth_wall"],
  "suggested_skill": "authentication_jwt", "evidence": "..."}]
```

- 报告：`Ben/report/writer.py` 增加 "Human review queue (highest miss-risk first)" 段，按分降序，阈值 `BEN_REVIEW_THRESHOLD=0.6`；TUI 可加 `review` tab。
- 与 M8 的关系：verify_finding 验证"已报的洞是不是假阳"，本评分标记"没报的面是否有假阴风险"，两者互补。

**验收**：构造三例（HTTPS 登录墙 / 同长空 body / base64 响应）→ 三者 score 均 ≥0.6 且 reason codes 正确；`review_queue.json` 降序；报告出现对应段落。

---

## 7. 实施顺序与验收口径

| 步骤 | 内容 | 依赖 | 验收 |
|---|---|---|---|
| 1 | `baseline.py` + `service_checks.json` + scout 子域名 | — | 清单条目可数、服务 check 展开正确 |
| 2 | `retry_queue.py` + 终态钩子（含 reaper） | 1 | kill/超时/stalled 三路径均入队；无 claimed 泄漏 |
| 3 | `reconcile.py` + finish 收尾顺序 | 1,2 | 注入漏项可被差集捕获并补扫 |
| 4 | task_graph 组链（groups/cursor/unlock_policy） | 1 | 前项失败后项照常解锁 |
| 5 | `miss_risk.py` + review_queue + 报告段 | 1,3 | 三特征样例评分正确、排序输出 |

**量化 DoD**：baseline 覆盖率（done/total）≥95% 或每项缺失有 reason；重试队列耗尽率 100%（成功或转 needs_follow_up）；差集补扫后 missing=0；组链完成率 100%；review queue 条数 ≤ 总项 20% 且全部带 reason codes。

**明确不动**：coverage 账本语义、task_claims TTL、scan_clock 残局闸门、verify 闭环、output_store 溢写机制。
