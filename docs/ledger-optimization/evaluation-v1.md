# 台账减量合理性评估（v1）

> 日期：2026-11-17。对象：[`README.md`](README.md)（方案 v2）。方法：对照审计/实测文档 + 代码写路径复核。  
> 本文件是评估结论的落盘件；**执行仍以 README 为唯一入口**。

---

## 结论

**可以减，且应该减。** 目标形态「业务台账 **7→4**，会话存档 3 件不动」成立；**不要**追求 Sapper 式「只剩 1 本」。

判据成立：「只有独立写入时机 + 独立 schema 才成一本」——约 19 本是视图/投影/潜伏代码，不该各自成权威源。真正要收敛的是**业务权威源重叠**，不是把 resume / SARIF / reports 砍掉。

---

## 代码复核

| 主张 | 判定 | 依据 |
|---|---|---|
| task_graph 单文件整写 + 进程内锁 | 成立 | `Ben/core/task_graph.py` 全量 `atomic_write_json` + `RLock` |
| P0 先于合并 | 成立 | 先合 claims/todos 进图会拉长持锁临界区 |
| coverage 工具空转、占 token | 成立 | `factory.py` 常挂；审计 11/11 `entries=0` |
| `run.json.tool_calls` 不能当观测源 | 成立（默认） | 仅 `BEN_RUN_RECORD_TIMINGS` 时写入；真源是 `agents.db` |
| claims 潜伏 | 成立 | `BEN_CLAIM` 默认 `"0"`，旁路不落盘 |
| verify_queue 可内联 | 部分成立 | **不是死代码**（`verify_finding` / `record_verify_outcome` 已注册）；内联须保住 finish 消费语义 |
| frontier 双轨 | 成立 | `Ben/core/frontier.py` 同时读 todos 与 intents → P3 双写再降只读正确 |
| 一节点一文件作 P0 | **否决** | 实测 1000 节点冷加载 426 ms vs 单文件 2.9 ms（退化 147×）；现行 P0 = **snapshot + WAL**（README §五、§六-7） |

---

## 为什么减本数合理

1. **成本不在磁盘，在权威分裂 + 工具 schema。** 投影全量重建约 16–17 ms；台账三组工具约 **3.8k tok/请求（~29% 静态输入）**。
2. **重复是真实的。** 一件待办：todos / intents / coverage 行 / claims；一条发现：vulns / notes[findings] / verify_queue。
3. **Sapper 对照说明方向，但不能照搬。** Ben 有 web / SARIF / resume（`agents.db`），多出的基础设施是产品契约。

---

## 目标 4 本

| 台账 | 评估 |
|---|---|
| `task_graph` | 必须保留并做收敛点；当前窗口期（0/20 run 启用） |
| `vulnerabilities.json` | 不能进图；可变生命周期 + SARIF/人审 |
| `threat_models.json` | 保留；方法视角，提示词依赖 |
| `notes.json`（禁 findings） | 保留；自由文本 ≠ 可判定事实 |

可降级：`coverage` 空转、`todos`↔intents 重复、`claims`/`verify_queue` 字段化、根 `skills/` 死重、潜伏 heartbeat/metrics。

---

## 风险（不否定减量）

1. **verify**：不是「0 使用即可删」，是「权威迁到 vuln 字段后删表」；漏 finish/validator 会静默丢验证。
2. **P2 `coverage_observed.jsonl`**：短期多一本 append-only 观测日志；按判据是基础设施，防文档口径膨胀成「第 5 本业务台账」。
3. **两套 claim**（`task_claims` vs `task_graph.claim_intent`）须在 P3 统一读者。
4. **未做真实 LLM run 验收**——P1/P2 须用真实 run 的 `cached_tokens` 与 entries>0 对账。
5. **对外话术**：说 **业务权威 7→4**，勿暗示 `run.json`/SARIF/`reports/`/`recon/` 消失。

---

## 评估立场

- **批准**减量目标与 P0–P6 主序（P0 = snapshot+WAL，**不是**一节点一文件）。
- **不批准**在 P0 前合并 claims/todos，或把 vulns/notes/resume 并进图。
- **优先落地**：P0 + P1 并行；P2 修好空转后再谈 baseline（P6）。

下一步实施清单见 [`p0-p1-impl-checklist.md`](p0-p1-impl-checklist.md)。
