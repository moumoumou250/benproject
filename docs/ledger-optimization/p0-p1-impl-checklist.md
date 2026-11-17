# P0 / P1 实施清单

> 依据：[`README.md`](README.md) §五 + [`evaluation-v1.md`](evaluation-v1.md)。  
> P0 与 P1 **相互独立，可并行**；本清单只拆这两阶段，P2 起另开。  
> **硬约束**：P0 之前不合并 claims/todos；P0 **不用**一节点一文件（用 snapshot + WAL）。

---

## P0 — task_graph：snapshot + WAL

### 目标与验收

| 项 | 目标 |
|---|---|
| 写 | 1k facts 下单次写 **≤ 5 ms**（期望 1–2 ms 常数） |
| 读/冷加载 | **≤ 10 ms**（期望 ≤ 8 ms） |
| 并发 | 8 写无长尾 |
| 兼容 | 旧 `task_graph.json` 仍可水合；新布局对调用方 API 不变 |

### 目标落盘形态

```
.state/<run>/.state/task_graph/
  snapshot.json     # facts + intents + dedupe_index + wal_seq
  wal.jsonl         # append-only 变更行；每行 fsync
  .compact.lock     # compact 互斥（可用 flock）
```

可选过渡：保留顶层 `task_graph.json` 为「最近一次 compact 的镜像」，但权威读写走目录布局。

### WAL 行约定（建议）

```json
{"seq": 1, "op": "upsert_fact", "id": "f…", "payload": {…}, "ts": …}
{"seq": 2, "op": "upsert_intent", "id": "i…", "payload": {…}, "ts": …}
{"seq": 3, "op": "set_dedupe", "key": "…", "fact_id": "f…", "ts": …}
{"seq": 4, "op": "delete_intent", "id": "i…", "ts": …}
```

- 写路径：内存 mutate → `append_jsonl(wal)`（已有 [`Ben/core/jsonstore.py`](../../Ben/core/jsonstore.py)）→ 更新内存 `wal_seq`；**不**每次全量 `atomic_write_json`。
- 读路径：`snapshot.json` → 按 `seq` 重放 WAL 尾部 → 填 `_facts` / `_intents` / `_dedupe_index`。
- Compact：每 **500** 条 WAL 或 run 结束；`atomic_write_json(snapshot)` → truncate/rotate WAL；持 `.compact.lock`。

### 改动文件

| 文件 | 动作 |
|---|---|
| [`Ben/core/task_graph.py`](../../Ben/core/task_graph.py) | `_path` → 目录根；`_persist_locked` 改 WAL append；`hydrate_task_graph_from_disk` 读 snapshot+WAL；加 `compact_locked()`；兼容旧单文件 |
| [`Ben/core/jsonstore.py`](../../Ben/core/jsonstore.py) | 复用 `append_jsonl` / `atomic_write_json`；如需目录 flock 可加小工具函数 |
| [`Ben/core/runner.py`](../../Ben/core/runner.py) | hydrate 路径不变（仍传 `state_dir`）；run 结束钩子调 compact |
| [`tests/core/test_task_graph.py`](../../tests/core/test_task_graph.py) | 现有行为单测保持；新增：WAL 追加后冷加载一致、compact 后 WAL 清空/截断、旧 `task_graph.json` 迁移读入 |
| `scripts/` 或 `/tmp` bench | 复测 1k facts 写/读延迟，对账 README §八 |

### 实现步骤

1. 引入 `_graph_dir` / `_wal_path` / `_snapshot_path`；`hydrate`：若存在目录布局用新逻辑，否则读旧 `task_graph.json` 并（可选）首次写时迁到目录。
2. 把所有 `_persist_locked()` 调用点改为「算 diff 或整对象 upsert 行 → append」。
3. 实现 `compact_locked()`；在 runner teardown / 阈值触发。
4. 锁策略：进程内仍用 `_lock` 护内存；WAL append 在锁内；compact 另加文件锁防双 compact。
5. 跑 `pytest tests/core/test_task_graph.py -q` + 微基准；对照验收表。

### 明确不做（P0）

- 不把 claims / todos / coverage 并进图
- 不改 agent 工具面（属 P1）
- 不引入一节点一文件

---

## P1 — 工具面瘦身（−1,583 tok/请求）

### 目标与验收

| 项 | 目标 |
|---|---|
| 删 `record_coverage` | −751 tok |
| todo 6→3 | −519 tok |
| notes 5→3 | −313 tok |
| 合计 | 台账工具面 **3,791 → ~2,208**；child 静态输入 ≤ **11.5K** |
| 单测 | 台账相关仍绿（原基线 52；允许因删工具调整断言） |
| 提示词 | **同步删**对已删工具的指引（README §六-8），否则净收益为负 |

### 保留 / 删除对照

**Coverage（3→2，删 create 侧）**

| 保留 | 删除 |
|---|---|
| `update_coverage`（纠偏，P2 仍要） | `record_coverage` |
| `list_coverage` | |

机器写入留给 P2；P1 只摘 agent 新建入口。临时空窗可接受（现状本就 entries=0）。

**Todo（6→3）** — token 账：去掉 `mark_todo_done`(102)+`mark_todo_pending`(113)+`delete_todo`(106)=321，余量靠收紧 `update_todo` docstring/schema 凑满 −519。

| 保留 | 删除（能力并入 `update_todo`） |
|---|---|
| `create_todo` | `mark_todo_done` → `update_todo` status=completed |
| `list_todos` | `mark_todo_pending` → status=pending |
| `update_todo` | `delete_todo` → status=cancelled 或显式 `op=delete` 字段 |

**Notes（5→3）** — token 账精确：`update_note`(252)+`delete_note`(61)=**313**。

| 保留 | 删除 |
|---|---|
| `create_note` | `update_note` |
| `list_notes` | `delete_note` |
| `get_note` | |

笔记改为 append-mostly；改正文用新 note 或等后续单轨。**P4 再禁 `category=findings`**，P1 不动该策略也可，但建议在 `create_note` 描述里弱化 findings 引导。

### 改动文件

| 文件 | 动作 |
|---|---|
| [`Ben/agents/factory.py`](../../Ben/agents/factory.py) | `_BASE_TOOLS` / `_COVERAGE_TOOLS` 去掉已删工具；import 同步 |
| [`Ben/agents/profiles.py`](../../Ben/agents/profiles.py) | recon/enum 清单去 `record_coverage`；文案改 `update_coverage` / 机器观测 |
| [`Ben/tools/coverage/tools.py`](../../Ben/tools/coverage/tools.py) | 删除或停用 `record_coverage`；`update`/`list` docstring 去掉「先 record」话术 |
| [`Ben/tools/todo/tools.py`](../../Ben/tools/todo/tools.py) | 扩展 `update_todo` 覆盖 done/pending/delete；移除三工具注册 |
| [`Ben/tools/notes/tools.py`](../../Ben/tools/notes/tools.py) | 移除 `update_note`/`delete_note` 注册（实现可暂留供测试/内部） |
| [`Ben/core/frontier.py`](../../Ben/core/frontier.py) | 文案：`record_coverage` → `update_coverage` |
| [`Ben/tools/finish/tool.py`](../../Ben/tools/finish/tool.py) | coverage reconciliation 步骤改写（无 record） |
| [`Ben/tools/agents_graph/tools.py`](../../Ben/tools/agents_graph/tools.py) | agent_finish 提示去 `record_coverage` |
| [`Ben/tools/verify/tool.py`](../../Ben/tools/verify/tool.py) | 提示去 `record_coverage` |
| [`Ben/skills/coordination/root_agent.md`](../../Ben/skills/coordination/root_agent.md) | 同步 |
| [`Ben/skills/analysis/counterevidence.md`](../../Ben/skills/analysis/counterevidence.md) | 同步 |
| [`Ben/skills/analysis/source_aware_discovery.md`](../../Ben/skills/analysis/source_aware_discovery.md) | 同步 |
| [`tests/agents/test_agent_tool_registration.py`](../../tests/agents/test_agent_tool_registration.py) | coverage 集合改为 `{update_coverage, list_coverage}` |
| [`tests/agents/test_profiles.py`](../../tests/agents/test_profiles.py) | 去 `record_coverage` 断言 |
| [`tests/agents/test_coverage_reconciliation_prompt.py`](../../tests/agents/test_coverage_reconciliation_prompt.py) | 断言新文案 |
| [`tests/agents/test_finish_coverage_gate.py`](../../tests/agents/test_finish_coverage_gate.py) | 按新 finish 文案调整 |
| [`tests/agents/test_agent_factory_tool_arguments.py`](../../tests/agents/test_agent_factory_tool_arguments.py) | parametrize 去掉 `record_coverage` / `update_note` |
| [`tests/tools/test_coverage_tool.py`](../../tests/tools/test_coverage_tool.py) | `record_coverage` 用例改为内部 helper 或标记删除 |
| [`tests/tools/test_todo.py`](../../tests/tools/test_todo.py) / [`test_notes.py`](../../tests/tools/test_notes.py) | 覆盖合并后的 update 路径；notes 删改测内部 `_impl` 即可 |

### 实现步骤

1. 先改 **skills + finish/frontier/profiles 文案**，再从 factory 摘工具，避免窗口期「提示词指向幽灵工具」。
2. todo：先增强 `update_todo`，再删三个 mark/delete 工具与注册。
3. notes：从 factory 去掉 `update_note`/`delete_note`；保留 `_update_note_impl` 仅当测试需要。
4. coverage：删除 `record_coverage` 注册与 `@function_tool`；重复检测文案改为「用 list + update」。
5. 跑：
   ```bash
   .venv/bin/python -m pytest tests/tools/test_todo.py tests/tools/test_notes.py \
     tests/tools/test_coverage_tool.py tests/core/test_task_graph.py \
     tests/core/test_task_claims.py tests/agents/test_agent_tool_registration.py \
     tests/agents/test_profiles.py tests/agents/test_coverage_reconciliation_prompt.py \
     tests/agents/test_finish_coverage_gate.py -q
   ```
6. 用既有 token 计量方式（bench B8 / tiktoken）复核 ≤ 2,208 tok 台账工具面。

### 明确不做（P1）

- 不上 `coverage_observed.jsonl`（P2）
- 不禁 notes findings（P4，可另 PR）
- 不删 `update_coverage`（机器化前的纠偏通道）

---

## 并行与合并建议

```text
分支 A: P0 WAL          ──┐
分支 B: P1 工具瘦身      ──┼──► main（无文件冲突预期：factory 与 task_graph 正交）
                          └──► 再开 P2
```

合并冲突热点若出现：通常在 `factory.py` import 区——P1 独占即可。

---

## 完成定义（Definition of Done）

- [ ] P0：1k facts 写 ≤ 5 ms、冷加载 ≤ 10 ms；旧单文件可读；`test_task_graph` 绿
- [ ] P1：`record_coverage` / 三 todo 标记工具 / `update_note`+`delete_note` 不再注册；skills 无残留调用名；相关单测绿
- [ ] 静态 token：台账工具面 ≈ 2.2k；无「提示词点名已删工具」
- [ ] README TL;DR 状态可改为「P0/P1 落地」并链到本清单勾选结果（实施 PR 内改，不在本评估回合）
