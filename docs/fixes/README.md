# 编排层改造 · 待修复清单

> 分支 `2026-11-14-id3r`，基线 commit `5a7b484`（"sapper编排"）。
> 本文是**唯一的待办清单**：修一项勾一项。论证过程与证据在 `docs/code-review-orchestration-v2.md`，这里只保留「改哪里 / 怎么改 / 怎么验」。
> 全部 30 项 = P0 九项（必修）+ P1 十三项（应修）+ P2 八项（空闲再修）。

---

## 0. 开工前必读

### 0.1 三条纪律

1. **不破坏可回滚性。** 所有闸门必须仍能用 `BEN_CLAIM` / `BEN_NO_PROGRESS_TURNS` / `BEN_SCAN_MINUTES` / `BEN_MAX_GENERALS` 关掉，关闭时行为与 `5a7b484` 完全一致。任何"顺手加个默认开启"的改动都不要做。
2. **一项一测试。** 每项的验收测试名都写在条目里，提交里必须能看到它。没有测试的"修好了"不算修好——这批 bug 的共同特征就是"看起来在工作"。
3. **先修 P0，再碰结构。** M10/M11 这类拆分重构放到最后一个 PR 单独做，否则 diff 会盖住真正的行为变化。

### 0.2 回归基线（重要）

```bash
.venv/bin/python -m pytest tests/core tests/tools tests/agents -q
```

当前有 **14 个既有失败，与本次改造无关，不要试图去修**：

- 12 个是测试 monkeypatch 了 `runner.run_dir_for`，而该符号在 `Ben/core/paths.py`，`runner` 从未导出过；
- 2 个 `tests/core/test_budget_pause_policy.py`：把 `hooks.py` / `frontier.py` 回退到 `HEAD~1` 仍失败，说明在 `45dca37` 就存在。

**修复完成的标准是：失败数仍是 14，且新增测试全绿。** 如果数字变了，先查是不是自己引入的。

### 0.3 四个可直接观测的验证口子

| 机制 | 观测点 | 怎么看 |
|---|---|---|
| claim | `run_dir/claims.json` + `owner_of(surface, risk)` | Ready 段是否还列着已认领的面 |
| 心跳 | `run_dir/heartbeat.jsonl` | 每行一个 `ts`，正常应逐轮增长 |
| 验证 | `run_dir/verify_queue.json` | `status` ∈ queued/upheld/refuted/insufficient/skipped_http |
| 指标 | `orchestration_metrics` JSON | `claim_ok` / `claim_reject` / `no_progress_injection` |

### 0.4 一览表

| # | 一句话 | 主改文件 | 改动量 | 阻断上线 |
|---|---|---|---|---|
| H1 | interactive 空闲会被 supervisor 误杀 | `supervisor.py` / `runner.py` | S | 是 |
| H2 | 卡死在 tool 里的 root，supervisor 停不住 | `supervisor.py` / `agents.py` | S | 是 |
| H3 | 一次模型异常让 in-flight 计数永久泄漏 | `supervisor.py` | S | 是 |
| H4 | verify 队列只写不读，refuted 照样进报告 | `tools/finish/tool.py` | M | 是 |
| H5 | `ruled_out_json` 写错就阻断 `agent_finish` | `agents_graph/tools.py` | S | 是 |
| H6 | 任何 agent 都能把自己的发现标成 upheld | `tools/verify/tool.py` | S | 是 |
| H7 | spawn 失败不释放 claim，该面锁死 67.5 分钟 | `execution.py` | S | 是 |
| H8 | `_finding_looks_http` 字段名错，跳过逻辑永不生效 | `tools/verify/tool.py` | XS | 是 |
| H9 | `create_agent` 不校验 `surface`/`risk_area`，claim 静默失效 | `agents_graph/tools.py` | XS | 是 |
| M1 | `[TIME]` 每轮追加不替换，模型读到过期值 | `hooks.py` | S | 否 |
| M2 | 全局状态跨 run 泄漏，第二次扫描一开局就"无进展" | `runner.py` | S | 否 |
| M3 | 正常 finish 被写成"无进展收尾" | `runner.py` | S | 否 |
| M4 | frontier 块重复渲染 + 6KB 尾部硬截断 | `frontier.py` | M | 否 |
| M5 | recall 归一化用 `min(len)`，短文档虚高 | `recall.py` | S | 否 |
| M6 | spawn 失败被吞成"已排队" | `tools/verify/tool.py` | XS | 否 |
| M7 | `verify_finding` 无幂等，重复派单 | `tools/verify/tool.py` | S | 否 |
| M8 | `except TypeError` 静默把 general 降级成 flash | `execution.py` | XS | 否 |
| M9 | `scan_clock` 用 monotonic，resume 时钟翻倍 | `scan_clock.py` | S | 否 |
| M10 | `hooks.py` 635 行职责膨胀 | `hooks.py` | L | 否 |
| M11 | 四份 atomic-write 副本，都没 fsync | 多文件 | M | 否 |
| M12 | 直读 `_todos_storage` / `_notes_storage` 私有属性 | `progress.py` / `recall.py` | M | 否 |
| M13 | 提示词删掉了三条操作性细节 | `system_prompt.jinja` | XS | 否 |
| L1–L8 | 一致性 / 可读性细节 | 见 §3 | — | 否 |

---

## 1. P0 必修（9 项）

### H1 · supervisor 会误杀 interactive 会话

**位置** `Ben/core/supervisor.py:98-115`（循环体）、`Ben/core/runner.py:1166-1173`（启动处）

**现象** `interactive=True` 时 root 常停在 `wait_for_user`，`coordinator.wait_kinds[root_id] == "user"`。此时没有 LLM 在飞、没有 ACTIVE 子 agent，而心跳只在 `on_llm_start` 写（`hooks.py:264`）→ 20 分钟后 `last_heartbeat_age_s()` 超阈值 → 判"卡死"，`cancel_descendants_graceful` 把整棵树停掉。用户离开 20 分钟回来，会话被判死。

**改法** 让 supervisor 认识"合法空闲"。循环体开头、`is_shutting_down` 之后插入：

```python
# 合法空闲：等用户、被人工/预算暂停，都不算卡死。
if coordinator.wait_kinds.get(root_id) in ("user", "agents"):
    continue
if coordinator.operator_paused or coordinator.budget_paused:
    continue
```

`wait_kind == "agents"` 表示在等子 agent，`has_active_child` 已经覆盖大部分情况，但落在这里更稳（子 agent 刚崩溃、状态还没刷新的那一拍）。

> 另一种更粗的做法是 `supervisor_task = None if self.interactive else ...`。**不推荐**：interactive 下 root 真的卡在 tool 里时也需要被救。要救就别关。

**验收** `tests/core/test_supervisor.py::test_interactive_idle_does_not_stop`

```python
# 构造 interactive coordinator，root 状态 waiting + wait_kinds["user"]，
# 心跳设成很久以前 → 跑 2 个 poll → 断言 statuses[root] 仍是 "waiting"
```

**风险** 低。纯跳过分支，不影响现有非交互路径。

---

### H2 · 卡在 tool 里的 root，supervisor 停不住

**位置** `Ben/core/supervisor.py:129-137`、`Ben/core/agents.py:551-561`（`request_stop`）、`:575-583`（`cancel_descendants_graceful`）

**现象（此处修正上一版审查的描述）** supervisor 实际调的是 `cancel_descendants_graceful(root_id)`，它会遍历整棵子树（**含 root 自身**）逐个 `request_stop`，而 `request_stop` 用的是 `stream.cancel(mode="after_turn")`。SDK 的语义是"跑完当前 turn 再停"（`result.py:734-745`）——也就是**必须等当前这轮 tool 调用返回**。而 supervisor 要救的恰恰是"tool 永不返回"，于是取消永远不会到达：状态改成 `stopped`、日志写了一条 warning、run 一直挂到外部超时。

**改法** 先礼后兵：graceful 之后给一个 grace 窗口，再升级为 `mode="immediate"`。

```python
# agents.py：给 request_stop 加一个 mode 参数（默认保持现有行为）
async def request_stop(self, agent_id: str, *, mode: str = "after_turn") -> None:
    ...
    if stream is not None:
        stream.cancel(mode=mode)
```

```python
# supervisor.py：替换现有 129-137
stopped = await coordinator.cancel_descendants_graceful(root_id)
await asyncio.sleep(stall_grace_s)                    # 默认 60s，给 tool 收尾
for aid in stopped:
    runtime = coordinator.runtimes.get(aid)
    if runtime is not None and runtime.stream is not None:
        runtime.stream.cancel(mode="immediate")
await coordinator.cancel_descendants(root_id)         # task.cancel()，兜底
await coordinator.set_status(root_id, "stopped",
                             error="supervisor: heartbeat stalled")
```

`cancel_descendants` 走的是 `task.cancel()`（`agents.py:563-573`），会向协程里抛 `CancelledError`，能中断任何 `await`。注意 `_start_child_runner` 的 `CancelledError` 分支要落终态（见 H7 的同类问题），否则快照里会留下 `running`。

**验收** `test_supervisor_stops_hung_root`

```python
# 一个 await asyncio.Event().wait() 永不返回的假 runtime.task + 陈旧心跳
# 跑 supervisor → 断言 task.done() 为 True 且状态为 stopped
```

**风险** 中。`immediate` 会跳过 session 落盘，可能丢最后一轮 usage 记账——所以必须先 graceful 再等 60s，只对真正超时的才 immediate。

---

### H3 · 一次模型异常就让 supervisor 永久失效

**位置** `Ben/core/supervisor.py:46-57`（计数器）、`Ben/core/hooks.py:268-273`（+1）、`:581-586`（-1）

**现象** `mark_llm_start()` 在 `on_llm_start` 末尾 +1，`mark_llm_end()` 在 `on_llm_end` 开头 -1。但 SDK **只在成功拿到响应时**才调 `on_llm_end`：

```python
# .venv/.../agents/run_internal/run_loop.py:1857-1871
if final_response is not None:
    ...
    await hooks.on_llm_end(context_wrapper, public_agent, final_response)
if not final_response:
    raise ModelBehaviorError("Model did not produce a final response!")
```

模型超时 / 5xx / 被取消 → 不调 `on_llm_end` → 计数永久 +1 → supervisor 的 `if llm_in_flight() > 0: continue` 从此不再放行。**一次网络抖动，这个 run 的看护就彻底失效**，且没有任何日志。

**改法** 计数器改成"带时间戳的集合"，把"陈旧在飞"视为不在飞。

```python
_llm_starts: set[float] = set()

def mark_llm_start() -> None:
    _llm_starts.add(time.monotonic())

def mark_llm_end() -> None:
    if _llm_starts:
        _llm_starts.discard(min(_llm_starts))     # 结束最早的那个

def llm_in_flight(*, stale_after_s: float | None = None) -> int:
    now = time.monotonic()
    if stale_after_s:
        for t in [t for t in _llm_starts if now - t > stale_after_s]:
            _llm_starts.discard(t)
    return len(_llm_starts)
```

supervisor 调用处改成 `llm_in_flight(stale_after_s=stall_timeout_s)`。另外在 `run_agent_loop` 的 `finally` 里调一次 `reset_supervisor_state()` 的"清计数"版本做双保险（保留 `_heartbeat_path`）。

**验收** `test_llm_inflight_survives_model_exception`

```python
# mark_llm_start() 后不调 mark_llm_end，直接时间旅行 stale_after_s=0 →
# 断言 llm_in_flight(stale_after_s=1) == 0
```

**风险** 低。`set[float]` 在同一时刻两次 start 会去重（monotonic 精度足够，但如果并发很高，改用 `list` + 索引）。若担心，用 `dict[int, float]` 自增 key。

---

### H4 · 验证队列零消费者，refuted 照样进报告

**位置** `Ben/tools/finish/tool.py:53-92`（`_do_finish` 落库段）

**现象** `tools/verify/tool.py` 认认真真写 `verify_queue.json`、认真花钱 spawn validator（每个 10 分钟并发额度），但**全仓没有任何读取方**：`list_verify_results` / `unfinished_verify_ids` 只在 `Ben/tools/verify/__init__.py` 里 re-export，`finish_scan` 完全不看。于是 `refuted` 的发现原样进正文，"对抗性验证"只产生了成本。这与我上一轮批评 recon"产出零消费者"是同一个模式，必须这次一起收掉。

**改法** 在 `_do_finish` 里、`update_scan_final_fields` **之前**加一道收口：

```python
from Ben.tools.verify.tool import list_verify_results

results = {str(r.get("finding_id")): r for r in list_verify_results()}
refuted: list[str] = []
unverified: list[str] = []
for report in report_state.vulnerability_reports:
    r = results.get(str(report.get("id")))
    if r is None:
        report["verification_status"] = "unverified"   # 没送验也算未验证
        continue
    status = r.get("status")
    if status == "refuted":
        report["verification_status"] = "refuted"
        refuted.append(str(report.get("id")))
    elif status == "upheld":
        report["verification_status"] = "upheld"
    else:
        report["verification_status"] = "unverified"
        unverified.append(str(report.get("id")))
```

配套三件事，缺一不可：

1. **报告渲染**：`verification_status == "refuted"` 的条目移出正文，进"已证伪 / 未采纳"附录；`unverified` 在标题上加 `[未验证]`。
2. **返回值**：`finish_scan` 的 result 里带 `refuted_ids` / `unverified_ids`，让 root 在最后一次工具调用里知道还欠什么。
3. **文档**：`verify/tool.py` 的模块 docstring（`:1-7`）现在写"fail-closed 的半边"，改完之后才是真的。

**验收** `test_verify_refuted_is_excluded_from_report`

```python
# 造 1 个 report(id="f1") + verify_queue 里 f1=refuted
# 调 _do_finish → 断言 f1 不在正文、在附录，且 result["refuted_ids"] == ["f1"]
```

**风险** 中。如果按 `effective` 口径统计漏洞数，`vulnerabilities_found` 会变小（这是**正确**的，但会让人以为"改造让发现变少了"）。建议同时输出 `vulnerabilities_found`（总数）与 `vulnerabilities_effective`（排除 refuted），别改口径。

> **替代方案**：如果这轮没时间做渲染改造，就把 `verify_finding` 的自动 spawn 关掉（只入队、不派单）。**半截闭环比没有更贵**——现在是在花钱买一个不进报告的结论。

---

### H5 · `ruled_out_json` 解析失败会阻断 `agent_finish`

**位置** `Ben/tools/agents_graph/tools.py:744-760`

**现象** JSON 非法直接 `return {"success": False, ...}`。此时子 agent **还没被标记 completed**，它以为终止失败，会继续跑、继续撞同一个格式错误，直到轮次耗尽——一次未转义的引号就能烧掉一个 12 轮的子 agent。

**改法** 可选字段的解析失败绝不允许阻断终止路径：

```python
parsed: list[dict[str, Any]] = []
if ruled_out_json and str(ruled_out_json).strip():
    try:
        parsed = json.loads(ruled_out_json)
    except json.JSONDecodeError as exc:
        logger.warning("agent_finish: ignoring malformed ruled_out_json: %s", exc)
```

**更好的做法（建议一次做掉）**：参数改成结构化类型，让模型填对象而不是手写转义 JSON：

```python
class RuledOutItem(BaseModel):
    surface: str
    risk_area: str
    evidence: str = ""

async def agent_finish(..., ruled_out: list[RuledOutItem] | None = None) -> str:
```

SDK 支持 pydantic 模型数组作为参数类型。改完之后 `ruled_out_json: str` 保留一个版本用于兼容，下一版删掉。

**验收** `test_agent_finish_tolerates_malformed_ruled_out`

```python
# ruled_out_json="{not json" → 断言 success 为 True 且状态变 completed
```

**风险** 低。丢掉的是"这次没记上的否决面"，比丢掉整次收口便宜得多。

---

### H6 · 任何 agent 都能把自己的发现标成 upheld

**位置** `Ben/tools/verify/tool.py:266-297`（`record_verify_outcome`）

**现象** 无归属校验：hunter 自己调一次就"通过验证"，root 也能批量洗白。这条工具和 `verify_finding` 都在 `_BASE_TOOLS`（`factory.py:1050-1051`）里，**所有角色都有**。对抗性验证退化成一个自证工具。

**改法**

```python
from Ben.core.agents import coordinator_from_context

entry = get_verify_result(finding_id)
if entry is None:
    return _err("unknown finding under verification")
coordinator = coordinator_from_context(inner)
tier = ((coordinator.metadata.get(me) or {}).get("tier")
        if coordinator is not None and isinstance(me, str) else None)
owner = entry.get("agent_id")
if owner and me != owner and tier != "validator":
    return _err("only the assigned validator may record this outcome")
```

顺带把 `verify_finding` 从 `_BASE_TOOLS` 挪到 **root 专属**工具集：验证什么由 root 决定，validator 只需要 `record_verify_outcome`。现在 hunter 能自己派单 + 自己能收单，两边都该收。

**验收** `test_record_verify_outcome_requires_ownership`

```python
# entry.agent_id="v1"，调用者 agent_id="h1" 且 tier="hunter" → success False
# 调用者 tier="validator" → success True
```

**风险** 低。但要注意：validator 是**另一个进程里的 agent**，`entry["agent_id"]` 在 spawn 成功后才写入（`tool.py:227-238`）。spawn 失败时 `agent_id` 为空，此时 `owner` 为假 → 放行，这是对的（没派出去的单允许 root 直接结算）。

---

### H7 · spawn 失败时 claim 泄漏，该面锁死 67.5 分钟

**位置** `Ben/core/execution.py:588-606`（`try_claim`）→ `:610-658`（`factory` / `register` / `_start_child_runner`）

**现象** claim 顺序在 factory 之前，之后三步任一抛错都会冒到 `create_agent` 的 `except`（只返回错误字符串给模型），**没有任何 release**。TTL = `(child_minutes or 45) * 1.5` ≈ 67.5 分钟。这段时间里 frontier 的 Ready 段会按 claims 过滤掉这个面（`frontier.py:237-248`），整个面派不出去，而且**没有任何日志说明为什么它不见了**。

**改法** 用 try/except 包住 spawn 的三步：

```python
try:
    child_agent = factory(**factory_kwargs)
    await coordinator.register(...)
    await _start_child_runner(...)
except BaseException:
    if surface_s and risk_s:
        from Ben.core.task_claims import release_for_agent
        release_for_agent(child_id)      # 只释放自己，不误伤兄弟
    await coordinator.set_status(child_id, "failed",
                                 error="spawn failed")   # 别留 running 僵尸
    raise
```

> 注意用 `BaseException` 而非 `Exception`：`CancelledError` 在 3.8+ 继承自 `BaseException`，用 `Exception` 会漏掉取消路径——正好是最常见的泄漏来源。
> 另一个等价方案是把 claim 拆成"内存预占 → register 成功后 commit"，改动更大，收益是彻底消除窗口期。

**验收** `test_claim_released_when_spawn_fails`

```python
# factory 抛 RuntimeError → 断言 create_agent 返回 success False
# 且 owner_of(surface, risk) is None
```

**风险** 低。`release_for_agent` 已按 agent_id 精确释放（`task_claims.py:186`）。

---

### H8 · `_finding_looks_http` 字段名错，HTTP 跳过逻辑永不生效

**位置** `Ben/tools/verify/tool.py:112-121`

**现象** 查的是 `http_request_id` / `exchange_id`，报告里实际字段是 **`http_exchange_ids`**（list，`report/state.py:416-417`）。结果：所有带代理证据的 HTTP 发现都会被派一个 10 分钟 validator，白烧一个并发额度（`BEN_MAX_SUBAGENTS=4` 时很贵）；而 docstring 承诺的"HTTP 已由 exchange-id 硬门覆盖"从未发生。

**改法**

```python
def _finding_looks_http(report: dict[str, Any]) -> bool:
    for holder in (report, report.get("metadata"), report.get("poc")):
        if not isinstance(holder, dict):
            continue
        ids = holder.get("http_exchange_ids")
        if isinstance(ids, list) and ids:
            return True
        if holder.get("http_request_id") or holder.get("exchange_id"):
            return True        # 保留旧字段兼容，别删
    return False
```

**验收** `test_verify_skips_http_findings`

```python
# report 带 http_exchange_ids=["123"] → verify_finding 返回 status=="skipped_http"
# 且未 spawn（spawner 的 fake 不被调用）
```

**风险** 低。反而会**减少**验证覆盖率——这是设计意图（HTTP 已有硬门），但要在报告里区分"未送验"与"送验被跳过"，否则审计时看起来像漏验。

---

### H9 · `create_agent` 不校验 `surface`/`risk_area`，claim 静默失效

**位置** `Ben/tools/agents_graph/tools.py:585-615`（参数校验块）、docstring `:557-565`

**现象** docstring 写"When `BEN_CLAIM=1`, required with `risk_area`"，代码里却没有任何校验；`execution.py:588` 是 `if surface_s and risk_s:` 才 claim。模型只要不传（很常见，参数是可选的），P0-1 就**静默关闭**，且没有任何提示——最坏的一种失效：没有报错，只是保护消失了。

**改法** 在 `valid_tiers` 校验之后插入：

```python
from Ben.core.task_claims import claims_enabled

if claims_enabled() and tier != "validator" and not (surface and risk_area):
    return json.dumps(
        {
            "success": False,
            "agent_id": None,
            "error": (
                "BEN_CLAIM is on: pass both surface (endpoint/route/component) "
                "and risk_area for this work unit, otherwise a sibling agent "
                "will test the same surface. For validation-only work use "
                "tier='validator'."
            ),
        },
        ensure_ascii=False,
        default=str,
    )
```

同时把 `Ben/skills/coordination/root_agent.md` 里那句"pass ... so the runtime can claim"改成硬规则（现在是建议语气）：

```markdown
- 每次 create_agent **必须**同时给 `surface` 与 `risk_area`（`BEN_CLAIM=1` 时缺一即报错）。
  `surface` 写最小工作单元（endpoint / route / 组件），不要写整站。
```

**验收** `test_create_agent_requires_surface_when_claims_on`

```python
# monkeypatch BEN_CLAIM=1，tier="hunter"，不传 surface → success False
# tier="validator" 不传 → 放行
```

**风险** 低，但**会改变模型行为**（更多工具报错）。建议先只对 `tier in ("hunter", "general")` 强制，`scout` 豁免（侦察阶段还不知道 surface 是正常的）。

---

## 2. P1 应修（13 项）

### M1 · `[TIME]` 每轮追加，不替换旧值

**位置** `Ben/core/hooks.py:293-316`（`_maybe_warn_scan_time`）

每轮 `input_items.append(...)`。12 轮留下 12 条 `[TIME] remaining=…`，模型完全可能引用早期那条**已经过期**的剩余时间（"还剩 40 分钟"而实际只剩 5 分钟）。同时它绕开了 frontier 已有的"同标记替换 + 签名节流"机制。

**改法** 二选一，推荐前者：

```python
# 方案 A：并进 frontier 块首行（frontier 已有 inject_or_replace_frontier，:346-370）
#   render_frontier_block 开头加 "- 时间：remaining=32m / total=90m [URGENT]"
# 方案 B：给 time 行独立 marker，复用同样的替换逻辑
TIME_MARKER = "[Scan time]"
for i, item in enumerate(input_items):
    if TIME_MARKER in _item_text(item):
        input_items[i] = {"role": "user", "content": line}
        return
input_items.append({"role": "user", "content": line})
```

**验收** 12 轮后 `input_items` 里 `[TIME]` 只出现 1 次。

---

### M2 · 进程级全局状态跨 run 泄漏

**位置** `Ben/core/progress.py:28`、`Ben/core/orchestration_metrics.py:33`、`Ben/tools/verify/tool.py:31`（三个 reset 函数），`Ben/core/runner.py:761-776`

`reset_progress_tracker()` / `reset_orchestration_metrics()` / `reset_verify_store()` 全仓**没有生产调用**，只在测试里出现。同一进程连跑两次扫描（interactive 下很常见）→ 第二次一开始 `is_no_progress_latched()` 就是 `True`，`stopped_reason` 直接写成"连续 N 轮无增量而收尾"，指标也是累加的。

**改法** 在 `runner.run_async` 的初始化段、`bind_*` **之前**显式写死顺序：

```python
# 顺序不能改：reset → hydrate → bind
reset_progress_tracker()
reset_orchestration_metrics()
reset_verify_store()
reset_supervisor_state()
reinstate_excluded_coverage(...)     # 现有 hydrate 段
bind_orchestration_metrics(run_dir)
bind_heartbeat(run_dir)
```

并在 `Ben/core/README.md`（新建）或各模块 docstring 写清"单进程单 run"假设。

**验收** `test_second_run_starts_with_clean_progress`

```python
# 跑一次 latch_no_progress() → 调 runner 初始化辅助函数 → 断言未 latch
```

---

### M3 · `stopped_reason` 判定顺序错，正常完成被误标

**位置** `Ben/core/runner.py:1204-1218`

先判 `is_no_progress_latched()`，再判 `root_status == "completed"`。模型收到 NO-PROGRESS 指令后**正常 `finish_scan`**，报告仍然写"连续 3 轮无增量而收尾"——审计时看起来像被强停的。另外 `no_progress_threshold()` 为 0 时文案变成"连续 0 轮无增量"。

**改法**

```python
if root_status == "completed":
    stopped_reason = "模型调用 finish_scan 完成扫描"
elif is_no_progress_latched() or "heartbeat stalled" in str(root_err):
    n = no_progress_threshold()
    if "heartbeat" in str(root_err):
        stopped_reason = "supervisor: heartbeat stalled"
    elif n > 0:
        stopped_reason = f"连续 {n} 轮无增量而收尾"
    else:
        stopped_reason = "无进展收尾"
elif ...
```

配套：`latch_no_progress()` 加 `reason="streak"|"heartbeat"|"clock"` 参数，文案按 reason 分支，别再用字符串匹配 `root_err`。

**验收** `test_completed_run_is_not_labeled_no_progress`

---

### M4 · frontier 块 Ready 与 gaps 重复，且 6KB 是尾部硬截断

**位置** `Ben/core/frontier.py:237-250`（Ready）、`:272-280`（gaps）、`:318-320`（截断）

同一批 `needs_follow_up` 行被渲染两遍（Ready 带 `suggest:` 两行，gaps 再来一次）。既费 token 又让 6KB 更容易打满；而截断是 `text[:max_chars]`，被砍掉的正好是**末尾的 todos / Sub-agents**——恰恰是编排最需要的两段。

**改法** 两件事：

1. 删掉 `## Coverage gaps` 段（Ready 已覆盖，gaps 只保留一个计数 `gaps_total=N`）。
2. 改**分段预算**，不要整块截断：

```python
budgets = {"ready": 2400, "claimed": 900, "todo": 1200, "agents": 1200}
# 每段超预算就折叠成 "- … N more；call list_coverage / list_todos"
# 关键段（todo / agents）永不整体丢弃
```

**验收** `test_frontier_block_marks_todos_even_when_budget_tight`

```python
# 造 40 个 todo + 8 个 agent → 断言输出里仍有 "## Open todos" 与 "## Sub-agents"
```

---

### M5 · recall 用 `min(len)` 归一化，短文档虚高

**位置** `Ben/core/recall.py:30-36`、阈值 `:15`

`inter / min(len(query), len(doc))`：一条 3 个词的 note 命中 1 个词就得 0.33，逼近阈值 0.34；CJK 按字切分后更糟（10 字笔记全中就是 1.0）。结果 spawn 前拼进去的"历史经验"大量是噪声。而且 `format_recall_block` 只输出 title，模型看到"有一行字很像"而不是"试过什么、结论是什么"，实际帮助有限。

**改法**

```python
def overlap_score(query: set[str], doc: set[str]) -> float:
    if not query or not doc:
        return 0.0
    inter = len(query & doc)
    if inter == 0:
        return 0.0
    base = inter / len(query)
    length_penalty = min(1.0, len(doc) / 12)      # 太短的 doc 打折
    return base * length_penalty
```

- `_RECALL_MIN_SCORE` 调到 0.45；
- `_coverage_docs()` 里跳过 `len(body) < 5` 的条目；
- 输出加一行 `evidence[:120]`，别只给 title。

**验收** `test_recall_ignores_tiny_notes`：`query="auth bypass"`、`doc="auth"` → score < 0.45。

---

### M6 · `verify_finding` 的 spawn 失败被吞成"已排队"

**位置** `Ben/tools/verify/tool.py:238-249`

spawner 抛错时返回 `success: True, status: "queued"`。`BEN_MAX_SUBAGENTS` 打满（4 个在飞是常态）时几乎必然如此，root 却以为验证已安排，报告里这条发现仍然"经过验证"。更要命的是返回文案写着"finish_scan will treat as unverified"——而 H4 没修之前，它根本不会。

**改法**

```python
except Exception as exc:
    logger.exception("verify_finding spawn failed for %s", finding_id)
    return json.dumps({
        "success": False,
        "finding_id": finding_id,
        "status": "not_spawned",
        "spawn_error": str(exc),
        "message": ("Not queued for verification: no child slot or spawn error. "
                    "Either wait for a slot and retry, or accept this finding "
                    "as unverified."),
    }, ensure_ascii=False)
```

`_verify_store` 里这条仍留 `queued`（下次可以重试），但返回值必须诚实。

**验收** `test_verify_spawn_failure_is_reported`

---

### M7 · `verify_finding` 无幂等

**位置** `Ben/tools/verify/tool.py:184-200`（无入口检查）

同一 `finding_id` 连调两次 → 两次入队 + 两次 spawn；第二个 validator 用 `surface=title, risk_area="validation"` 会撞上第一个的 claim，必然失败一次（白跑一个 10 分钟额度）。

**改法** 入口处先查：

```python
existing = get_verify_result(finding_id)
if existing and existing.get("status") in {"queued", "upheld", "refuted"}:
    return json.dumps({"success": True, "already": True, **existing},
                      ensure_ascii=False)
```

只允许 `insufficient` / `not_spawned` / `skipped_http` 重新派单。

**验收** `test_verify_is_idempotent`：连调两次，spawner 只被调用一次。

---

### M8 · `except TypeError` 回退会掩盖真实错误

**位置** `Ben/core/execution.py:610-621`

```python
try:
    child_agent = factory(**factory_kwargs)
except TypeError:
    child_agent = factory(name=name, skills=skills)   # 静默降级
```

工厂现在已支持 `model_name` / `use_root_capabilities`，这个回退没有存在必要；更糟的是它会把 factory 内部真实的 TypeError 也吞掉，并把 `general` **静默降级成 flash**——质量事故，且没有一行日志。

**改法** 直接删掉回退。若确实要兼容旧工厂，用签名探测：

```python
params = inspect.signature(factory).parameters
if "model_name" not in params:
    logger.warning("factory does not accept model_name; general tier degrades to flash")
    factory_kwargs.pop("model_name", None)
    factory_kwargs.pop("use_root_capabilities", None)
child_agent = factory(**factory_kwargs)
```

**验收** `test_general_tier_factory_error_is_not_swallowed`

---

### M9 · `scan_clock` 用 monotonic，resume 会静默翻倍

**位置** `Ben/core/scan_clock.py:37-46`（`install_deadline`）、`:48-56`（`remaining_seconds`）

把 `time.monotonic() + total*60` 写进 context。`-r` 续跑时 `install_deadline` 重新执行 → 得到**全新的完整时钟**，原 run 已用掉的时间不算了。一个 90 分钟的扫描续跑三次就能跑 4 小时，而 `BEN_SCAN_MINUTES` 的语义是"整个任务的总预算"。

**改法** 存墙钟：

```python
context[SCAN_STARTED_AT_KEY] = started_at           # epoch 秒
context[SCAN_DEADLINE_KEY] = started_at + total * 60.0
```

`remaining_seconds` 用 `time.time()` 计算；resume 时从 `run.json` 继承原始 `started_at`；需要加时走显式 `BEN_SCAN_MINUTES_EXTEND`。

**验收** `test_scan_clock_resume_keeps_original_deadline`

---

### M10 · `hooks.py` 职责膨胀到 635 行

**位置** `Ben/core/hooks.py`（635 行，单个 `ReportUsageHooks`）

一个类同时管：预算三档、turn 三档、软轮次、frontier 注入、时间行、无进展、心跳、usage 记账、工具计时。每加一个信号都要改这个类，测试要构造整个 hooks。更根本的问题是语义冲突：`budget_policy="pause"` 的契约是"永不告诉 agent 有上限"，而 `[TIME]` / `[NO-PROGRESS]` 必须告诉它——两类信号混在一个类里，正是 `test_budget_pause_policy` 那两个既有失败暴露的东西。

**改法** 拆两个，runner 里组合：

```python
class BudgetHooks:        # on_llm_start 只做预算/turn/usage 记账
class OrchestrationHooks: # frontier / time / no-progress / heartbeat
class CompositeHooks(BudgetHooks, OrchestrationHooks): ...
```

顺序上 `BudgetHooks` 先跑（预算能直接中断），编排类只读不阻断。

**验收** 现有 `tests/core/test_hooks.py` / `test_budget_pause_policy.py` 全绿（后者的 2 个既有失败**允许保持失败**，但要确认没有新增）。

---

### M11 · 四份 atomic-write 副本，都没 fsync

**位置** `task_claims._persist_locked`、`orchestration_metrics._persist_locked`、`verify/tool._persist_locked`、`supervisor.write_heartbeat`（这个是 append）

**改法** 抽 `Ben/core/jsonstore.py`：

```python
def atomic_write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)

def append_jsonl(path: Path, obj: Any) -> None: ...
```

**这不是洁癖**：claim 丢失意味着两个 agent 可能同时开工，是唯一"写入丢失会直接造成重复劳动"的账本，必须 fsync。其余三处顺带统一。

**验收** `test_jsonstore_atomic_roundtrip`（含写入后 `os.stat().st_size > 0`）。

---

### M12 · 私有属性耦合，改结构就静默失效

**位置** `Ben/core/progress.py:96-105`（直读 `todo_tools._todos_storage`）、`Ben/core/recall.py:60-76`（直读 `notes_tools._notes_storage`）

这两个存储的 shape 一变，`except Exception` 会吞掉并返回空 → 签名永远不变 → **误判"无进展"并提前收尾**。这是最坏的失效方向：静默、且造成损失（少扫一大片）。

**改法** 给 todo/notes 各加公开读取函数：

```python
# Ben/tools/todo/tools.py
def list_all_todos() -> list[dict[str, Any]]: ...
# Ben/tools/notes/tools.py
def list_notes() -> list[dict[str, Any]]: ...
```

`progress` / `recall` 只依赖公开 API；`except Exception` 至少 `logger.warning` 一次（用模块级 flag 防刷屏）。

**验收** `test_progress_signature_never_swallows_storage_errors`：猴子补丁让存储抛错 → 断言有 warning 且**不** latch。

---

### M13 · 提示词删掉了几句操作性细节

**位置** `Ben/agents/prompts/system_prompt.jinja`

瘦身效果是真的（deep child 9,339 → 6,813 tok，−27%），但删掉的不全是废话：

| 删掉的 | 后果 |
|---|---|
| "非交互长任务放后台、重定向到日志文件再读回" | 模型更容易把 `nmap -p-` 跑成超时 |
| "超长输出会被截断，可按需缩小范围重读" | 模型会反复重跑同一条命令取全量输出 |
| "HTTPS 目标经 Caido 代理时浏览器证书已信任" | 模型自己加 `--ignore-certificate-errors` 或去折腾证书 |

**改法** 每处补一句压缩版（各 ~15 字），总成本 < 60 token，换掉三类可复现的行为回归。这是**性价比最高的一项**：60 token 换掉三类常见浪费。

**验收** 跑 3 次 `nmap -p-` 类任务，观察是否还出现"命令超时 + 重复重跑"。

---

## 3. P2 空闲再修（8 项）

| # | 问题 | 位置 | 建议 |
|---|---|---|---|
| L1 | `get_args(Literal[...])` 每次重算；`_VALID_TIERS` 两处各定义一份 | `agents_graph/tools.py:595`、`execution.py:448` | 提到 `Ben/core/tiers.py` 单一来源 |
| L2 | 每轮重复读 coverage / report_state（progress + heartbeat + frontier 各一次） | `hooks.py:_write_heartbeat`、`progress.py` | 每轮取一次快照，作为参数传下去 |
| L3 | 每个 root 工具调用都整文件重写 metrics JSON（同步 IO 在事件循环里） | `orchestration_metrics.record_root_tool_call` | 内存计数 + 每 30s / 结束时 flush |
| L4 | `_suggest_line` 用 `{name!r}` 生成 Python repr，surface 含引号时出现 `name="a'b"` | `frontier.py:174-183` | 统一 `json.dumps(x, ensure_ascii=False)` |
| L5 | claim 默认 TTL 67.5 是魔数 | `task_claims.py:139` | 命名常量或从 `BEN_SUBAGENT_MINUTES * 1.5` 推导 |
| L6 | 模块 docstring 说"fail-closed 的半边"，实际未闭环 | `tools/verify/tool.py:1-7` | 修完 H4 再改；否则先写"只入队，不阻断报告" |
| L7 | 新模块缺"已知限制"（TTL 泄漏、无 fsync、单进程假设） | 各新模块 docstring | 各补 2-3 行 |
| L8 | `todo` 新增 pending 不计入签名，"只做计划不动手"会被判无进展 | `progress.py:94-107` | 有意为之就写进 docstring；否则把 todo 总数纳入签名 |

---

## 4. 建议的 PR 拆分

按"一个 PR 一条失效链路"切，四个 PR，每个都能独立回滚。

### PR-A · 守护链路（H1 + H2 + H3 + M2 + M3）

标题：`fix(supervisor): 不再误杀空闲会话，能真正停住卡死 root`

- supervisor 认识 `wait_kind=user` 与 paused；
- `request_stop(mode=...)` 支持 immediate，supervisor 先礼后兵；
- in-flight 计数改时间戳集合 + 陈旧淘汰；
- runner 初始化段补 `reset_*` 序列；
- `stopped_reason` 判定顺序修正。
- 新测试 4 个（H1/H2/H3/M2）。

### PR-B · 验证闭环（H4 + H5 + H6 + H8 + M6 + M7）

标题：`feat(verify): 验证结果接入 finish_scan，验证器不可自证`

- `finish_scan` 消费 verify 队列，refuted 移出正文；
- 报告渲染区分 upheld / unverified / refuted；
- `record_verify_outcome` 加归属校验，`verify_finding` 挪回 root；
- 修 `http_exchange_ids` 字段；
- spawn 失败诚实返回、入口幂等；
- `agent_finish` 容忍 malformed `ruled_out_json`。
- 新测试 5 个。

### PR-C · claim 稳定性（H7 + H9 + M8 + M5）

标题：`fix(claim): spawn 失败释放认领，缺 surface 时明确报错`

- try/except 包 spawn 三步 + `release_for_agent`；
- `create_agent` 强制 surface/risk（validator/scout 豁免）；
- 删 `except TypeError` 静默降级；
- recall 归一化修正 + 输出 evidence。
- 新测试 3 个。

### PR-D · 结构与细节（M1 + M4 + M9 + M10 + M11 + M12 + M13 + L1-L8）

标题：`refactor(orchestration): 拆 hooks、统一 jsonstore、修时间注入`

这一批**没有行为变更的优先级**，但 M1/M13 会影响模型行为，建议排在最后，单独观察一轮。

---

## 5. 完成定义（DoD）

全部勾选才算这轮改造收尾：

- [ ] `tests/core tests/tools tests/agents` 失败数 = 14（既有），新增测试全绿
- [ ] `BEN_CLAIM=0` / `BEN_NO_PROGRESS_TURNS=0` / `BEN_SCAN_MINUTES` 未设时，行为与 `5a7b484` 一致（跑一次 blackbox 短扫描对比 `run.json` 关键字段）
- [ ] 交互式会话空闲 25 分钟不被停（手动验证一次，最直观）
- [ ] `finish_scan` 的返回里能看到 `refuted_ids` / `unverified_ids`
- [ ] kill -9 中途后 `-r` 续跑：剩余时间**不**回升，已测面不重测，已 upheld 的发现不重派验证
- [ ] 一次真实扫描里 `owner_of` 无泄漏（run 结束后 claims.json 只剩未完成的认领，且都被 TTL 覆盖）
- [ ] `docs/` 下三份文档（`sapper-vs-ben`、`sapper-orchestration`、`code-review-v2`）里的结论与最终实现不矛盾；有出入的在 `docs/fixes/` 里留一行"已修正"

---

## 6. 改动地图（谁改哪个文件）

| 文件 | 涉及项 |
|---|---|
| `Ben/core/supervisor.py` | H1 H2 H3 M2 |
| `Ben/core/agents.py` | H2（`request_stop` 加 mode） |
| `Ben/core/runner.py` | H1 H2 M2 M3 |
| `Ben/core/execution.py` | H7 M8 L1 |
| `Ben/core/hooks.py` | H3 M1 M2 M10 |
| `Ben/core/progress.py` | M2 M3 M12 L8 |
| `Ben/core/frontier.py` | M4 L4 |
| `Ben/core/recall.py` | M5 M12 |
| `Ben/core/scan_clock.py` | M9 |
| `Ben/core/task_claims.py` | H7 L5 |
| `Ben/core/orchestration_metrics.py` | M2 M11 L3 |
| `Ben/core/jsonstore.py` | M11（新建） |
| `Ben/tools/verify/tool.py` | H4 H6 H8 M6 M7 L6 |
| `Ben/tools/finish/tool.py` | H4 |
| `Ben/tools/agents_graph/tools.py` | H5 H9 L1 |
| `Ben/skills/coordination/root_agent.md` | H9 |
| `Ben/agents/prompts/system_prompt.jinja` | M13 |
| `docs/fixes/README.md` | 本文件（修一项勾一项） |
