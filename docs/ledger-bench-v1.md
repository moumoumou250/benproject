# 台账方案实测与迭代（v1）

> 日期：2026-11-17。约束：**未修改任何项目代码**；基准脚本置于 `/tmp/ledger_bench/`，所有写盘落在 `/var/tmp`；调用的是项目自身的 `jsonstore` / `task_graph` / `coverage` / `todo` / `notes` / `report.*` 实现。
> 环境：Linux x86_64，Python 3.12.16（项目 `.venv`），agents SDK 0.19.4，本地 NVMe。
> 被测对象：`docs/ledger-audit-v1.md` 与 `docs/sapper-ledger-comparison-v1.md` 提出的 P1–P4 方案。

## 一、怎么测

7 组基准 + 1 组单测：

| 编号 | 测什么 | 方法 |
|---|---|---|
| B1 | 原子写成本 | `atomic_write_json` 不同载荷各写 10 次 |
| B2 | **task_graph 单文件写放大** | 100/300/600/1000 facts，逐个 `add_fact`，累计磁盘字节 |
| B3 | coverage 单次记录成本 | 200 次 `_record_impl`，含重复拒绝 |
| B4 | todo / notes 单次写成本 | 100 次真实工具调用（ToolContext） |
| B6 | 水合成本 | 600 facts + 200 coverage + 100 todos 重启加载 |
| B7 | 真实 run 派生产物 | 复制 3 个真实 run，跑 `read_agent_graph` / `build_coverage_document` / `write_vulnerabilities` / `write_sarif` |
| B8 | **工具面 token 成本** | tiktoken o200k 统计 14 个台账工具的描述 + JSON Schema |
| B10 | 机器推导 coverage 的数据源 | 直读 `agents.db`，统计消息数、体积、解析耗时 |
| — | 功能基线 | 5 个台账单测文件（todo/coverage/notes/task_graph/claims） |

## 二、实测结果

### B1 原子写：fsync 是地板，不是体积

| 载荷 | 1KB | 50KB | 200KB | 512KB | 2MB |
|---|---|---|---|---|---|
| 每次写 | 3.77 ms | 4.19 ms | 5.96 ms | 7.72 ms | 17.99 ms |

→ 4 ms 左右的 fsync 地板；200KB 以内几乎免费。

### B2 task_graph：写放大随规模平方增长（最关键的发现）

| 规模 | 单次 add（前 50） | 单次 add（后 50） | 最终文件 | 累计写入 | 放大倍数 |
|---|---|---|---|---|---|
| 100 facts | 5.4 ms | **8.6 ms** | 77 KB | 4.0 MB | 50× |
| 300 facts | 5.3 ms | **22.7 ms** | 231 KB | 35.5 MB | 150× |
| 600 facts | 6.2 ms | **43.3 ms** | 462 KB | 142 MB | 300× |
| 1000 facts | 5.6 ms | **51.1 ms** | 771 KB | **395 MB** | **500×** |

→ 每次写都要重写整图；1000 facts 时一次写 51 ms、800KB，且**全程持锁**。若多 agent 共享，等于把所有 agent 的写操作串行在一个 51ms 的临界区上。按曲线外推 2000 facts ≈ 100 ms/写、1.6 GB/千写。

### B3 / B4 单本台账的单次写成本

| 台账 | 单次写 | 文件规模 | 写放大 |
|---|---|---|---|
| coverage（200 条） | 0.6–3.9 ms（fsync 抖动） | 19.4 KB | 200× |
| todo（100 条） | 4.07 ms（p95 5.6） | 32.8 KB | 50× |
| notes（100 条） | 4.01 ms | 60.8 KB | — |

### B6 水合：很便宜

600 facts 7.0 ms / 200 coverage 1.2 ms / 100 todos 0.03 ms / 全部状态 493 KB。

→ resume 的瓶颈不在水合，别往这里优化。

### B7 真实 run 的派生产物周期

| run | agents | 覆盖条目 | 漏洞 | 一次完整重建（coverage+vulns+sarif） |
|---|---|---|---|---|
| ben_2026…2b4f004c | 2 | 0 | 1 | **16.4 ms** |
| 117-72-199-2-3000_972f | 2 | 0 | 1 | **17.4 ms** |
| pictbland-net_d217 | 2 | 0 | 0 | **17.4 ms** |

→ 一次 `_write_artifacts()` 约 16–17 ms。即使每 run 触发 100 次，也只有 1.7 秒。**“投影降频”几乎不值钱。**
→ 同时确认：20 个 run 里 9 个在 `copytree` 阶段 `PermissionError`（`nobody:nogroup`），台账“写了读不了”是真实缺陷。

### B8 工具面 token（每个请求都要付）

| 工具 | tokens | | 工具 | tokens |
|---|---|---|---|---|
| `record_coverage` | **751** | | `create_todo` | 342 |
| `update_coverage` | 478 | | `list_todos` | 183 |
| `list_coverage` | 273 | | `update_todo` | 300 |
| `create_note` | 416 | | `mark_todo_done` | 102 |
| `list_notes` | 332 | | `mark_todo_pending` | 113 |
| `get_note` | 82 | | `delete_todo` | 106 |
| `update_note` | 252 | | `delete_note` | 61 |
| **coverage 三件套** | **1,502** | | **todo 六件套** | **1,146** |
| | | | **notes 五件套** | **1,143** |

**三组台账工具合计 3,791 tokens/请求**。对照 `prompt-slimming-audit.md` 的 child 系统提示 9,339 tokens：静态输入 13,130，**台账工具占总静态输入的 28.9%**（占提示词本身的 40.6%）。而 `coverage_tools` 默认 `True`，root 与 child 都挂。

### B10 机器推导 coverage 的真实数据源

`run.json.tool_calls` 在全部 run 中为 **0**（真实工具调用只在 `agents.db`）。实测 agents.db：35–114 条消息、49–229 KB、全量 JSON 解析 **0.5–1.3 ms**。

→ 数据侧很便宜，但消息是自然语言 + 命令文本，抽取 surface 需要规则/模型，准确性是主要风险。

### 功能基线

```
52 passed in 0.74s   (tests/tools/test_todo.py, test_coverage_tool.py, test_notes.py,
                      tests/core/test_task_graph.py, test_task_claims.py)
```

→ 台账模块本身是健康的，问题在“用不用、怎么用”，不在 bug。

## 三、实测推翻了原方案的什么

| # | 原判断 | 实测 | 结论 |
|---|---|---|---|
| 1 | “投影每状态全量重写”是主要浪费（P4 降频） | 一次完整重建 16–17 ms，100 次才 1.7 s/run | **高估 IO、低估 token**：降频降为顺手项 |
| 2 | 台账成本主要在磁盘与工具调用 | 工具 schema 3,791 tok/请求 ≈ 29% 静态输入 | **真正的成本是工具面，不是 IO**；P1 要改成“先砍 schema” |
| 3 | 先把 claims/todos/coverage 全并进 task_graph（P2 先行） | 单文件写放大 500×、1000 facts 时 51 ms 持锁 | **合并会放大写放大**：必须先改存储形态，再合并 |
| 4 | 机器推导 coverage 从 `run.json.tool_calls` 取 | `tool_calls` 恒为 0，真源是 `agents.db` | 数据源判断错误；且应改为**工具边界埋点**而非事后解析 |
| 5 | resume 优化方向包括水合 | 600 facts 水合 7 ms | 水合不是瓶颈；resume 成本在 session 回放 |

## 四、迭代后的方案（v2）

### 顺序与依据

| 顺序 | 动作 | 实测依据 | 预期效果 |
|---|---|---|---|
| **P0** | `task_graph` 改 **一节点一文件 + flock**（或追加日志 + 周期压实） | B2：1k facts 51 ms/写、800KB/写、395MB/千写 | 单次写回到 **1–4 ms 常数**；放大 500×→~1×；锁内时间 51 ms→<1 ms |
| **P1** | **工具面瘦身**：删 `record_coverage`（−751 tok）、todo 6→3（−519）、notes 5→3（−313） | B8 逐工具实测 | **−1,583 tok/请求（−12% 静态输入）**；按 200 请求/run 估算 −32 万 tokens/run |
| **P2** | coverage 改**机器推导**：在工具边界埋点（不为 coverage 单独开写口），agents.db 兜底回放 | B10：解析 1.3 ms；B3：手写 200 条仅 4MB，价值不在 IO | 覆盖从 0 变真实；再省 agent 决策轮次；`update_coverage` 只留纠偏 |
| **P3** | 待办单轨：claims 内联 intent、todos 只读 | B4：todo 单写 4 ms、50× 放大（绝对量小，问题是语义） | 一套状态机；必须在 P0 之后，否则每次 claim 更新都重写整图 |
| **P4** | 发现单源：notes 禁 findings、verify 内联 vuln | B7：vuln 写 8–10 ms，可接受 | 一条发现一个权威源；`verification_status` 不再外挂 |
| **P5** | 卫生：UID/chown、死 skills、文档 | B7：9/20 run 宿主不可读 | 台账可审计；删 100KB 死重 |
| **P6** | baseline / reconcile | 前置条件全部就绪 | 防漏扫挂在真实 coverage 上 |
| ~~P4'~~ | ~~投影降频~~ | B7：16 ms/周期 | **降级为顺手做，不单独立项** |

### 与 v1 的差异（一句话）

v1 说“先机器化 coverage、再收敛待办”；实测表明**先改 task_graph 的存储形态、再砍工具面**，否则收敛动作会把 51 ms 的写锁变成整条链路的瓶颈，而 coverage 空转的主要代价其实是那 751 个 token 的工具描述。

## 五、本测试的边界

- **没有跑真实 LLM 扫描**（无授权目标/密钥），所以 token 数是**静态 schema 计量**，不是账单；账单侧应用 `usage.py` 的 `cached_tokens` 在真实 run 上复核。
- IO 数据来自本地 NVMe；容器 overlay 上会更慢，但结论方向不变（fsync 地板在容器里通常更高，进一步支持“少而小”的写）。
- 未测多 agent 并发下的锁争用（需要压测 spawn）；B2 的 51 ms 持锁是**单线程**数据，并发下只会更差，不会更好。
- 机器推导 coverage 的**语义准确率**未测，需要 2–3 个 run 的人工标注对照；这也是 P2 保留 `update_coverage` 纠偏通道的原因。

---

*基准脚本：`/tmp/ledger_bench/bench3.py`（B1/B2/B3/B6）、`bench4.py`（B4/B7/B8/B10）。复跑方式：`.venv/bin/python /tmp/ledger_bench/bench4.py`。*
