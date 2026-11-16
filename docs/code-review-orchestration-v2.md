# 编排层改造代码审查（commit `5a7b484 sapper编排`）

审查范围：16 个源文件改动 + 7 个新增模块（`task_claims` / `progress` / `scan_clock` / `supervisor` / `recall` / `orchestration_metrics` / `tools/verify`）。
方法：逐文件读实现 + 对照 SDK 生命周期源码 + 跑目标测试 + 实测提示词 token。
**未修改任何代码**，本文只给结论与改法。

---

## 0. 总体结论

方案落地得比预期完整：claim 语义、图签名停机、角色档位、就绪/认领分段、run 级时钟、心跳、验证队列都建起来了，工厂的 `_FACTORY_MODEL_SENTINEL` 设计尤其干净（能区分"没传"和"传了 None"）。

但有三类问题必须先修：

1. **守护机制在最需要它的场景下失效**（H1/H2/H3）：supervisor 会误杀 idle 的交互会话，却救不了真正卡死的 root，而且一次模型异常就能让它的 in-flight 计数永久泄漏。
2. **验证闭环只建了一半**（H4/H6/H8）：`verify_queue.json` 只写不读，refuted 的发现照样进报告；任何 agent 都能自证 upheld；HTTP 证据字段名写错导致跳过逻辑永不生效。这和我上一轮批评 recon "产出零消费者"是同一个模式。
3. **P0-1 的效果会被两个细节归零**（H7/H9）：spawn 失败不释放 claim（该面锁死 67.5 分钟），`create_agent` 不强制 `surface`/`risk_area`（模型不传就静默不认领）。

测试侧：`tests/core/` 有 14 个失败，我逐个查了原因，**全部是既有失败**，与本次改动无关——
- 12 个是测试 monkeypatch 了 `runner.run_dir_for`（该符号在 `Ben/core/paths.py`，runner 从未导出过）；
- 2 个 `test_budget_pause_policy` 我把 `hooks.py`/`frontier.py` 临时回退到 `HEAD~1` 后仍失败，确认是 frontier 注入落地（`45dca37`）时就已存在。
新增测试 87 + 4 个（verify_queue / tier_concurrency）全绿。

---

## 1. 高优先级（正确性 / 功能缺口）

### H1｜supervisor 会误杀 interactive 会话

- **现象**：`runner.py:1163-1176` 无条件启动 supervisor；`interactive=True` 时 root 常在 `wait_for_user` 上停着，此时没有 LLM 调用在飞、没有 active child，而心跳只在 `on_llm_start` 写（`hooks.py:296-340`）→ 20 分钟后 `last_heartbeat_age_s()` 超阈值 → supervisor 判定"卡死"，`set_status(root_id,"stopped")` 并取消全部子 agent。用户离开 20 分钟回来，会话被判死。
- **证据**：`supervisor.py:98-138`（循环体无 interactive / wait_kind 判断）；`runner.py` 启动处无 `self.interactive` 分支。
- **修法**：

```python
# supervisor.run_supervisor 循环内
if coordinator.wait_kinds.get(root_id) == "user":
    continue                      # 等用户在 interactive 下是正常状态
if coordinator.operator_paused or coordinator.budget_paused:
    continue
```

```python
# runner.py
supervisor_task = None if self.interactive else asyncio.create_task(...)
```

### H2｜supervisor 停不住真正卡死的 root

- **现象**：只调 `coordinator.set_status(root_id, "stopped", ...)`（`supervisor.py:131`）。`set_status` 只改字典状态，不取消 stream/task（对比 `agents.py:551-561 request_stop` 才 `stream.cancel(mode="after_turn")`）。root 若卡在一个永不返回的 tool call（正是要救的场景），状态变了、run 还挂着，直到外部超时。
- **修法**：

```python
await coordinator.request_stop(root_id)        # after_turn 取消
await asyncio.sleep(grace_s)                   # 给当前 tool 收尾
await coordinator.cancel_descendants(root_id)
task = coordinator.runtimes.get(root_id)
if task is not None and task.task and not task.task.done():
    task.task.cancel()
```

### H3｜`mark_llm_start` / `mark_llm_end` 不配对，一次异常就让 supervisor 永久失效

- **现象**：`hooks.py:269-272` 在 `on_llm_start` 末尾 +1，`hooks.py:583-586` 在 `on_llm_end` 开头 -1。但 SDK 只在**成功拿到响应**时调 `on_llm_end`：

```python
# .venv/.../agents/run_internal/run_loop.py:1857-1869
if final_response is not None:
    ...
    await hooks.on_llm_end(context_wrapper, public_agent, final_response)
if not final_response:
    raise ModelBehaviorError("Model did not produce a final response!")
```

模型超时 / 5xx / 取消 → 不调 `on_llm_end` → `_llm_in_flight` 永远 +1 → `supervisor` 的 `if llm_in_flight() > 0: continue` 从此不再放行。
- **修法**：改成带时间戳的集合，把"陈旧在飞"视同不在飞。

```python
_llm_starts: set[float] = set()

def mark_llm_start() -> None:
    _llm_starts.add(time.monotonic())

def mark_llm_end() -> None:
    if _llm_starts:                 # 粗粒度：结束一个最早的
        _llm_starts.discard(min(_llm_starts))

def llm_in_flight(*, stale_after_s: float | None = None) -> int:
    now = time.monotonic()
    if stale_after_s:
        for t in [t for t in _llm_starts if now - t > stale_after_s]:
            _llm_starts.discard(t)
    return len(_llm_starts)
```

supervisor 调 `llm_in_flight(stale_after_s=stall_timeout_s)`。另建议在 `on_agent_end`（或 run 的 `finally`）里整体清零，双保险。

### H4｜验证队列零消费者，fail-closed 没有实现

- **现象**：`tools/verify/tool.py` 写 `verify_queue.json`，但全仓没有任何读取方——`list_verify_results` / `unfinished_verify_ids` 只在 `Ben/tools/verify/__init__.py` 里被 re-export；`finish_scan`（`tools/finish/tool.py:18-90`）完全不看它。于是 `refuted` 的发现照样出现在最终报告里，"对抗性验证"只产生了调用成本。
- **修法**（在 `_do_finish` 的 `update_scan_final_fields` 之前加一道收口）：

```python
from Ben.tools.verify.tool import list_verify_results

results = {str(r.get("finding_id")): r for r in list_verify_results()}
refuted, unverified = [], []
for report in report_state.vulnerability_reports:
    r = results.get(str(report.get("id")))
    if r is None:
        continue
    if r.get("status") == "refuted":
        report["verification_status"] = "refuted"
        refuted.append(report.get("id"))
    elif r.get("status") in {"queued", "insufficient"}:
        report["verification_status"] = "unverified"
        unverified.append(report.get("id"))
```

配套：报告渲染时 `verification_status == "refuted"` 的条目不进正文（改列"已证伪/未采纳"附录），`unverified` 在标题上标"[未验证]"。`finish_scan` 的返回里带 `refuted_ids` / `unverified_ids`，让 root 知道还欠什么。

### H5｜`ruled_out_json` 解析失败会阻断 `agent_finish`

- **现象**：`tools/agents_graph/tools.py:744-756`，JSON 非法直接 `return {"success": False, ...}`。此时子 agent **还没被标记 completed**，它会以为终止失败继续跑，反复撞同一个格式错误直到轮次耗尽。
- **修法**：可选字段的解析失败绝不能阻断终止路径。

```python
if ruled_out_json and str(ruled_out_json).strip():
    try:
        parsed = json.loads(ruled_out_json)
    except json.JSONDecodeError as exc:
        logger.warning("agent_finish: ignoring malformed ruled_out_json: %s", exc)
        parsed = []
```

更好的做法：把参数改成结构化类型 `ruled_out: list[RuledOutItem] | None`（SDK 支持 pydantic 模型数组），让模型填对象而不是转义 JSON 字符串——现在这个设计对模型很不友好，长 evidence 里一个未转义的引号就废掉整次收口。

### H6｜`record_verify_outcome` 没有归属校验（fail-open）

- **现象**：`verify/tool.py:267-298`，任何 agent 都能把任意 `finding_id` 标成 `upheld`。hunter 自己调一次就"通过验证"；root 也能批量洗白。这条工具还在 `_BASE_TOOLS`（`factory.py:1050-1051`），所有角色都有。
- **修法**：

```python
entry = get_verify_result(fid)
if entry is None:
    return _err("unknown finding under verification")
me = inner.get("agent_id")
tier = (coordinator.metadata.get(me) or {}).get("tier")
if entry.get("agent_id") and me != entry["agent_id"] and tier != "validator":
    return _err("only the assigned validator may record this outcome")
```

顺带把 `verify_finding` 从 `_BASE_TOOLS` 挪到 root 专属工具集（让 root 决定验证什么），validator 只需要 `record_verify_outcome`。

### H7｜spawn 失败时 claim 泄漏，该面锁死 67.5 分钟

- **现象**：`execution.py:589-606` 先 `try_claim(...)`，之后才 `factory()` → `register()` → `_start_child_runner()`。这三步任一抛错都会冒到 `create_agent` 的 `except`（返回错误字符串给模型），但**没有任何 `release`**；TTL = `(child_minutes or 45)*1.5` ≈ 67.5 分钟。这段时间里 frontier 的 Ready 段会把这个面过滤掉（`frontier.py:225-232` 按 claims 过滤），整个面派不出去。
- **修法**：

```python
try:
    child_agent = factory(**factory_kwargs)
    await coordinator.register(...)
    await _start_child_runner(...)
except BaseException:
    if surface_s and risk_s and claims_enabled():
        from Ben.core.task_claims import release_for_agent
        release_for_agent(child_id)
    raise
```

或者把 claim 拆成"预占（内存）→ commit（register 成功后落盘）"两段。

### H8｜`_finding_looks_http` 字段名错，HTTP 跳过逻辑永不生效

- **现象**：`verify/tool.py:112-121` 查 `http_request_id` / `exchange_id`；报告里实际字段是 **`http_exchange_ids`**（`report/state.py:416-417`，一个 list）。结果：所有带代理证据的 HTTP 发现都会被派一个 10 分钟的 validator，白烧一个并发额度（`BEN_MAX_SUBAGENTS=4` 时很贵）；而 docstring 承诺的"HTTP 已由 exchange-id 硬门覆盖"从未发生。
- **修法**：

```python
ids = report.get("http_exchange_ids")
if isinstance(ids, list) and ids:
    return True
```

再加一条测试：带 `http_exchange_ids=["123"]` 的报告必须返回 `skipped_http`。

### H9｜`create_agent` 不强制 `surface`/`risk_area`，claim 形同虚设

- **现象**：`tools/agents_graph/tools.py:557-565` 的 docstring 写"When `BEN_CLAIM=1`, required with `risk_area`"，但代码没有任何校验；`execution.py:588` 是 `if surface_s and risk_s:` 才 claim。模型只要不传（很常见，因为参数是可选的），P0-1 就静默关闭，而且没有任何提示。
- **修法**：

```python
from Ben.core.task_claims import claims_enabled
if claims_enabled() and tier != "validator" and not (surface and risk_area):
    return json.dumps({
        "success": False,
        "error": "BEN_CLAIM is on: pass surface (endpoint/route/component) and "
                 "risk_area so the work unit can be claimed; otherwise two agents "
                 "will test the same surface.",
    }, ensure_ascii=False)
```

并在 `root_agent.md` 的派发流程里把"必须带 surface/risk_area"写成硬规则（现在只说了"pass ... so the runtime can claim"，语气是建议）。

---

## 2. 中优先级（设计 / 稳健性）

### M1｜`[TIME]` 每轮追加，不替换旧值

`hooks.py:_maybe_warn_scan_time` 每轮 `input_items.append(...)`。12 轮会留下 12 条 `[TIME] remaining=…`，模型可能引用早期那条已经过期的剩余时间；而且它绕开了 frontier 的"同标记替换"节流机制。
**修法**：把时间行并进 `[Frontier status]` 块首行（`frontier.py` 已有签名节流 + 替换），或给 time 行加独立 marker 做 replace。

### M2｜进程级全局状态跨 run 泄漏

`progress._no_progress_latched` / `_stall_streak` / `_last_sig`、`orchestration_metrics._metrics` 在 `runner` 启动时**没有重置**（全仓搜不到 `reset_progress_tracker` / `reset_orchestration_metrics` 的生产调用；`bind_orchestration_metrics` 只设路径）。同一进程连跑两次扫描（interactive 下很常见）→ 第二次一开始 `is_no_progress_latched()` 就是 True，最终 `stopped_reason` 直接写成"连续 N 轮无增量而收尾"，指标也是累加的。
**修法**：`runner.run_async` 初始化段调用 `reset_progress_tracker()` + `reset_orchestration_metrics()`（放在 `bind_*` 之前），并把它们纳入现有的 hydrate 序列。

### M3｜`stopped_reason` 判定顺序错，正常完成会被误标

`runner.py:1204-1218` 先判 `is_no_progress_latched()`，再判 `root_status == "completed"`。模型收到 NO-PROGRESS 指令后正常 `finish_scan`，报告仍写"连续 3 轮无增量而收尾"；另外 `no_progress_threshold()` 为 0 时文案变成"连续 0 轮无增量"。
**修法**：completed 优先；latch 时记原因（`latch_no_progress(reason="streak"|"heartbeat"|"clock")`），文案按原因分支，阈值为 0 时不产出该文案。

### M4｜frontier 块 Ready 与 gaps 重复，且 6KB 是尾部硬截断

`frontier.py` 现在依次输出 Ready（含 suggest 两行）→ Claimed → **Coverage gaps（同一批行再列一遍）** → Counts → todos → agents。重复渲染既费 token，也让 6KB 更容易被打满；而截断是 `text[:max_chars]`（`:340` 附近），被砍掉的正好是末尾的 todos / Sub-agents —— 恰恰是编排最需要的两段。
**修法**：删掉 gaps 段（Ready 已覆盖），按段预算（例如 Ready 2.5KB / Claimed 1KB / todo+agents 2.5KB，超了各自折叠成计数），不要整块截断。

### M5｜`recall.overlap_score` 用 `min(len)` 归一化，短文档虚高

`recall.py:30-36`：`inter / min(len(query), len(doc))`。一条 3 个词的 note 命中 1 个词就能得 0.33，接近阈值 0.34；CJK 按字切分后更严重（10 字笔记全中就是 1.0）。而且 `format_recall_block` 只输出 title，没有 evidence，模型拿到的是"有一行字很像"而不是"试过什么、结论是什么"。
**修法**：`inter / len(query)` 为主 + 长度惩罚；要求 `len(doc) >= 5`；输出加一行 `evidence[:120]`；阈值调到 0.45 再观察召回质量。

### M6｜`verify_finding` 的 spawn 失败被吞成"已排队"

`verify/tool.py:238-249`：spawner 抛错时返回 `success: true, status: "queued"`。`BEN_MAX_SUBAGENTS` 打满（4 个在飞是常态）时几乎必然如此，root 却以为验证已安排，`finish_scan` 里这条发现仍然"经过验证"。
**修法**：返回 `"status": "not_spawned"` 且 `success: false`，文案给 root 两个选项（等 slot / 直接标 unverified）。

### M7｜`verify_finding` 无幂等

同一 `finding_id` 连调两次 → 两次入队 + 两次 spawn；第二个 validator 用 `surface=title, risk_area="validation"` 会撞上第一个的 claim，必然失败一次（白跑）。
**修法**：入口先查 `get_verify_result(fid)`，`queued/upheld/refuted` 直接返回既有状态，不重复派单。

### M8｜`_factory` 的 `except TypeError` 回退会掩盖真实错误

`execution.py:615-621`：

```python
try:
    child_agent = factory(**factory_kwargs)
except TypeError:
    child_agent = factory(name=name, skills=skills)   # 静默降级
```

工厂现在已支持 `model_name` / `use_root_capabilities`，这段回退没有存在必要；更糟的是它会把 factory 内部的 TypeError（真 bug）也吞掉，并把 `general` 静默降级成 flash——质量事故且**没有一行日志**。
**修法**：直接删掉回退；若确实要兼容旧工厂，用 `inspect.signature(factory).parameters` 探测，并在降级时 `logger.warning`。

### M9｜`scan_clock` 用 `time.monotonic()` 存进 context，resume 会静默翻倍

`scan_clock.py:37-46` 把 `time.monotonic() + total*60` 写进 context。`-r` 续跑时 `install_deadline` 重新执行 → 得到**全新的完整时钟**（原 run 已用掉的时间不算了）。若将来把 context 落快照，monotonic 跨重启也不可比。
**修法**：存 `started_at_epoch + total_minutes`（wall clock），剩余时间用 wall clock 算；resume 时从 `run.json` 继承原始 `started_at`，需要加时就显式 `BEN_SCAN_MINUTES_EXTEND`。

### M10｜`hooks.py` 职责膨胀到 630 行

一个 `ReportUsageHooks` 同时管：预算三档、turn 三档、软轮次、frontier 注入、时间行、无进展、心跳、usage 记账、工具计时。每加一个信号都要改这个类，且测试要构造整个 hooks。
**建议**：拆成 `BudgetHooks`（预算/turn/usage）与 `OrchestrationHooks`（frontier/time/progress/heartbeat），在 runner 里组合成一个 `CompositeHooks`；`hooks.py` 保持只做预算，符合它原来的名字。

### M11｜四处重复的 atomic-write 实现，且都没有 fsync

`task_claims._persist_locked`、`orchestration_metrics._persist_locked`、`verify/tool._persist_locked`、`supervisor.write_heartbeat` 各写了一遍 tempfile+replace（heartbeat 是 append）。
**建议**：抽 `Ben/core/jsonstore.py`：`atomic_write_json(path, obj)` + `append_jsonl(path, obj)`，统一 `flush + os.fsync`（现在断电可能丢最后一次 claim 写入，而 claim 丢失意味着两个 agent 可能同时开工）。

### M12｜私有属性耦合，改结构就静默失效

`progress.py:96-105` 直接读 `todo_tools._todos_storage`，`recall.py:60-76` 直接读 `notes_tools._notes_storage`。这两个存储的 shape 一变，`except Exception` 会吞掉并返回空 → 签名永远不变 → **误判"无进展"并提前收尾**（这是最坏的失效方向：静默、且造成损失）。
**建议**：给 todo/notes 各加公开读取函数（`list_all_todos()` / `list_notes()`），progress/recall 只依赖公开 API；`except Exception` 至少 `logger.warning` 一次。

### M13｜提示词删掉了几句操作性细节，有行为回归风险

这次瘦身效果确实明显（见 §5），但删掉的不全是废话：

- "非交互长任务放后台重定向到日志文件再读回" → 删了之后模型更容易把 `nmap -p-` 这类命令跑成超时；
- "超长输出会被截断，可按需缩小范围重读" → 删了之后模型可能反复重跑同一条命令取全量输出；
- "HTTPS 目标经 Caido 代理时浏览器证书已信任" → 删了之后可能自己给 agent-browser 加 `--ignore-certificate-errors` 或去折腾证书。

**建议**：每处补一句压缩版（各 ~15 字），总成本 <60 token，换掉三类常见行为回归。

---

## 3. 低优先级（可读性 / 一致性 / 细节）

| # | 问题 | 位置 | 建议 |
|---|---|---|---|
| L1 | `get_args(Literal[...])` 每次调用重算；`_VALID_TIERS` 在 execution 与 tools 各定义一份 | `tools.py:595`、`execution.py:448-459` | 提到 `Ben/core/tiers.py` 单一来源，两处 import |
| L2 | 每轮重复读 coverage / report_state（progress + heartbeat + frontier 各一次） | `hooks.py:_write_heartbeat`、`progress.py` | 每轮取一次快照，作为参数传下去 |
| L3 | 每个 root 工具调用都整文件重写 metrics JSON（同步 IO 在事件循环里） | `orchestration_metrics.record_root_tool_call` | 内存计数 + 每 30s/结束时 flush |
| L4 | `_suggest_line` 用 `{skill!r}`/`{name!r}` 生成 Python repr，surface 含引号时出现 `name="a'b"` | `frontier.py:174-183` | 统一 `json.dumps(x, ensure_ascii=False)` 生成 |
| L5 | claim 默认 TTL 67.5 是魔数 | `task_claims.py:139` | 从 `BEN_SUBAGENT_MINUTES * 1.5` 推导，或作为常量命名 |
| L6 | 模块 docstring 与实现不符：说"fail-closed 的半边"，实际未闭环 | `verify/tool.py:1-7` | 修完 H4 再改文档；否则先写"目前只入队，不阻断报告" |
| L7 | 新增模块缺"已知限制"说明（TTL 泄漏、无 fsync、单进程假设） | 各新模块 docstring | 各补 2-3 行，省下后来者的排查时间 |
| L8 | `todo` 新增 pending 不计入签名，"只做计划不动手"会被判无进展 | `progress.py:94-107` | 有意为之就写进 docstring；否则把 todo 总数也纳入签名 |

---

## 4. 结构层面的三条建议（比单点修复更值钱）

1. **给验证补上消费者，否则不如不做**：`verify_finding` 已经在花真钱派子 agent。要么按 H4 接进 `finish_scan`，要么先关掉自动 spawn（只入队，等有消费方再开）。半截闭环比没有更贵。
2. **建立"全局状态生命周期"约定**：现在 4 个新模块都用模块级 dict + `hydrate/reset` 三件套，但 reset 只在测试里调。建议在 runner 的初始化顺序里显式写成 `reset → hydrate → bind`，并在 `Ben/core/README` 或模块 docstring 里写清"单进程单 run"的假设。
3. **把"预算类"与"编排类"信号分开**：预算管的是"还能不能花钱"，编排管的是"还该不该继续干"。混在一个 hooks 类里，`budget_policy="pause"`（"永不告诉 agent 有上限"）与 `[TIME]`/`[NO-PROGRESS]`（必须告诉它）在语义上冲突——这也正是 `test_budget_pause_policy` 那两个既有失败暴露的问题。

---

## 5. 做得好的部分（不要回退）

- **提示词瘦身实测有效**（同一脚本、同口径，o200k）：

| 变体 | 改前 | 改后 | 变化 |
|---|---|---|---|
| deep child | 9,339 | **6,813** | −27% |
| deep root | 10,836 | 10,156 | −6% |
| blackbox child | — | **5,484** | — |

  技能目录改成"按类只列 name"是最大贡献；工具段改成"路由表"也让重复付费消失了。
- **`_FACTORY_MODEL_SENTINEL`** 解决了"不传参数"与"显式传 None"的歧义，比 `model_name=None` 直接当哨兵正确得多。
- **claim 键规范化与 coverage 一致**（都是 `strip().lower()`），两个账本不会打架——这个细节很容易漏。
- **`progress` 用内容签名而不是 mtime/长度**，避免了"写了个文件就算有进展"的假阳性。
- **失败即降级的路线没有走偏**：所有新增闸门都能用 env 关掉（`BEN_CLAIM` / `BEN_NO_PROGRESS_TURNS` / `BEN_SCAN_MINUTES` / `BEN_MAX_GENERALS`），回滚成本低。

---

## 6. 建议补的 5 个测试（按 H 顺序）

1. `test_supervisor_interactive_idle_does_not_stop`：interactive + `wait_kind=user` + 心跳停止 → 不应 set_status。
2. `test_supervisor_stops_hung_root`：root 卡在未返回的 tool → 断言 stream 被 cancel / task 结束。
3. `test_llm_inflight_survives_model_exception`：模拟 model 抛异常后 `llm_in_flight()` 仍会归零（或被视为陈旧）。
4. `test_verify_refuted_is_excluded_from_report`：refuted 的 finding 不进正文/被标 unverified。
5. `test_claim_released_when_spawn_fails`：factory 抛错后 `owner_of(...) is None`。

---

## 7. 关于"和代码助手交互是否该改用英文省 token"

**结论：不必。** 你的提问语言在编码会话的总 token 里占比很小，换成英文省下的是零头，代价是表达精度和阅读成本。

粗略量级（一次 20 轮的编码会话）：

| 项 | 典型量 | 换英文能省 |
|---|---|---|
| 你的提问 | 20 × 100~300 tok ≈ 2~6K | ~30~40%（≈1~2K） |
| 助手回答 | 20 × 300~800 tok ≈ 6~16K | ~30~40%（中文输出在西文分词器上更贵） |
| 仓库上下文（文件读取、grep 结果） | 50~200K | 0 |
| 工具输出（日志、编译错误） | 20~100K | 0 |

即总账上省 **不到 2%**，而"仓库上下文 + 工具输出"占八九成——它们不由你的提问语言决定。

**值得用英文的三处**：
1. **要落盘、要长期维护的长文本**（设计文档、规格、commit message、代码注释）——写进仓库就是长期资产，跟团队/工具链语言一致更重要；
2. **单段超过 2K token 的原创长文**且目标模型是 Claude/GPT 这类西文分词器——这一段能省 30~50%，是唯一"看得见"的收益；
3. **需要模型精确复用术语或格式**时（schema、报错文本、正则），英文歧义更小。

**不值得的三处**：
- DeepSeek / GLM / Qwen 路由（中文分词已优化，收益接近 0，术语还可能被译歪）；
- 已经命中 prompt cache 的前缀（换语言会让缓存失效，第一次全价反而不划算）；
- 短问答、追问、报错粘贴——省几十 token，沟通成本更高。

**一条纪律**：不要在同一个会话里来回切语言。缓存前缀、术语一致性、你自己回看时的可读性都会受损。要切就按"这条消息的产物是否进仓库"来切，而不是按"这轮想不想省钱"来切。
