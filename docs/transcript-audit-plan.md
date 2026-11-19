# 对话全量落盘方案 v2（verify 调试用）

状态：待批准（P0 已完成；P1–P4 未动手；2026-11-19 实测核查见 §0）  
日期：2026-11-19  
基线：以工作区实际代码为准（原写 `Ben/` @ f9b304d 不成立，见 §0.4）  
关联：`docs/skill-verification-plan-v2.md` §14（本文件是其增补的实现方案）

**取代**本文件 v1 稿的 P1/P2/P3 设计：记录点从「factory + execution 主循环各挂一处」改为**唯一的 SDK `RunHooks` 旁路**（§4.1 给了对照与理由）。v1 稿的 A/C 两项与脱敏规则保留，D 项（压缩前快照）因记录点变更而**取消**（§4.5）。

---

## 0. 现状核查（2026-11-19，实测结论）

本节是「方案写完了，代码动到哪」的唯一权威答案。方法：直接读代码 + 跑测试，不依赖记忆。

### 0.1 已完成

| 项 | 证据 |
|---|---|
| 本方案文档（合并版） | 本文件 §1–§13 |
| v2 的三处增补 | `skill-verification-plan-v2.md`：E0 阶梯（§5）、S4b 分期（§9）、D9/D10（§12）、验收三条（§10） |
| P0 只读导出 | `scripts/dump_transcript.py` **实测通过**：对 run `172-17-0-1-8083_5017` 导出 3 agent（Root 46 条 / Hunter 47 条 / Confirm 34 条）→ `_artifacts/transcripts/<run>/` |
| E0 不得进 CONFIRMED | `Ben/kb/verdict.py:74` 已硬拒 `ref.startswith("audit/")`；`Ben/kb/evidence.py:38` 已识别 `e0` kind |
| verify 主体（S1–S3 + S4 骨架） | `Ben/kb/` 2190 行；`pytest tests/kb` **30 passed**；`ben skill check/diagnose/verify` 三命令可用；`_execute_task` 重构已完成（`main.py:387`） |

### 0.2 未完成：P1–P4，代码 0 行

逐项实测均不存在（`ls` / `grep` 为空），与 §8 清单一一对应：

- `Ben/core/audit.py` — 不存在（writer / hooks / renderer 全缺）
- `Ben/core/hooks.py` — 无 `CompositeHooks`
- `Ben/core/runner.py` — 无 audit 接线、无 `finalize()`
- `Ben/interface/cli/transcript.py` — 不存在；`main.py` 无分发；`completions.py` 无注册
- `Ben/tools/output_store.py` — 无 spill 宿主机双写
- `paths_seen` / `blocked_snippets` 等派生视图 — 不存在（`grep` 命中的是 hooks 里无关的 `turns_used`）
- `verdict.json.transcript` 段 — 不存在
- `tests/core/test_audit.py` — 不存在

### 0.3 核查中新发现、方案原文未覆盖的缺口

按「会不会让人白跑一次扫描」排序，前三条都属于**开关已埋、但没人读**的空开关。

| # | 缺口 | 证据 | 为什么必须补 |
|---|---|---|---|
| **G1** | `BEN_AUDIT` 是空开关 | `kb/verify.py:159` 已 `setdefault("BEN_AUDIT","1")`，全仓**零处读取** | 现在 verify 已经在「假装录像」。P1 不接线，跑完看目录是空的，而这次扫描可能花了百万 token。P1 必须让该变量真正被 `AuditWriter` 读到，且 `BEN_AUDIT=0` 能关闭 |
| **G2** | `BEN_KB_EVIDENCE` 是空开关（E2 通道未实现） | `kb/verify.py:158` 已 setdefault，全仓零处读取；最新 run 目录无 `.evidence/` | **比 G1 更严重**：E2 是 CONFIRMED 的举证主力，通道不通则 `confirm()` 永远 `UNREADABLE`。v2 §9 S4 写了「runner.py 拷 `.evidence/`」，实际没做 |
| **G3** | `stopped_code` 未进 runner，靠 reason 字符串猜 | `runner.py` 无 `stopped_code`；`main.py:438` 用 `_stopped_code_from_reason()` 正则猜 | 子原因归属全靠它。文案一改（「预算耗尽」→「成本超限」），`budget` 会被误判成 `finished`，直接改写 verify 结论 |
| **G4** | 两份重复且不一致的映射 | `main.py:368` vs `kb/verify.py:48`；后者缺 `reason != "未知" → finished` 兜底，同一 reason 两处结论不同 | 两处结论不一致时，`verdict.json` 与日志会互相打脸，排查时先怀疑数据 |
| **G5** | `verify.py:247` 死代码 | `all(s in {...} for s in statuses if _norm_is_oracle(e := None, oracle_path) or True)`：`or True` 使条件恒真，`_norm_is_oracle` 恒 `False` 成死函数，`e := None` 无意义，整段 `if: pass` 什么都不做 | P4 要在此处接 `TECHNIQUE_MISS` 诊断。恒真条件会让**所有** 4xx 请求都进候选，诊断句将失去分辨力 |
| **G6** | `verify.py:368` 把靶场写死 | instruction 硬编码 `CVE-2022-22947 / gateway actuator SpEL` 与默认 `/actuator/gateway/routes` | 接第二个正样本（如 idor）时会告诉模型「目标已知存在 idor（CVE-2022-22947 / SpEL）」，直接误导。应从 profile 读，不设默认路径 |

次要两条（列入 P2/P4 顺手做）：

- **G7 目录双轨未定义**：P0 写 `_artifacts/transcripts/<run>/`，本方案写 `_artifacts/runs/<run>/audit/`。需明确 P0 是否迁入 audit、或并存并各自说明定位（建议：并存，P0 标注「legacy，仅 agents.db 视图」）。
- **G8 §6 表格与正文矛盾**：表格定义 `BEN_AUDIT_REDACT=1` 为「写前脱敏」，正文（§6 正文末）说「脱敏只发生在导出 `--redact`」。实施 P1 前必须二选一，否则写前脱敏会被误实现，毁掉 cookie/header 证据。

### 0.4 基线修正

§1 头部原写「基线 `Ben/` @ f9b304d」。实测工作区有 40+ 未提交改动（含 `Ben/kb/` 全部实现），此基线**不成立**。引用本方案时以「工作区实际代码」为准，不能按 f9b304d 回溯。

### 0.5 实测后果：落盘缺失已经造成一次错误判定（2026-11-19 09:32）

这不是假设，是当天跑出来的。链路上每一环都有实测证据：

| 环节 | 实测事实 |
|---|---|
| verify 判定 | `_artifacts/kb_evidence/20261119_093225_rce_local-scg-rce-positive/verdict.json`：`verdict=MISS_SURFACE`、`paths_seen=[]`、`hint=本 run 请求过：（无）；oracle 要 /actuator/gateway/routes/ben-kb-oracle` |
| 该目录内容 | 只有 `criteria.json / oracle / scan.json / verdict.json`——**没有 `http/`**（E2 未通，沙箱 `.evidence/` 随容器销毁） |
| 数据源 | `evidence.py:57 collect_from_http_dir()` 只读 `evidence_dir/http/`；空目录 → 零 `Evidence` → `paths_seen=[]` → `surface_hit=False` |
| 但 agents.db 里 | `f50e63c1` 的**第一个**工具调用就是 `exec_command → http://172.17.0.1:8083/actuator/gateway/routes/ben-kb-oracle`；`a24833fe` 之后又请求同一路径 2 次 |
| 反证 | 手工把 3 组 req/resp 放进 `rejudge_rce_scg/http/` 后，**同一 run** 重判得 `CONFIRMED`、`paths_seen=3` |

**结论**：判定逻辑本身没坏，坏的是「证据没有落盘」。而正确数据一直躺在 `agents.db` 的 `function_call.arguments` 里——只要把这些参数里的 URL 抽出来做 `paths_seen`，09:32 那次就会得到 `surface_hit=True`。

这条也是 P1 之外的**最小止血补丁**（不依赖整个 audit 链路）：给 `paths_seen` 加 agents.db fallback，读 `agents.json` 拿 agent id，扫 `.state/agents.db` 的 `function_call` 参数抽 URL。约 30 行，能让 verify 在 E2 未通时不再误判 `MISS_SURFACE`。

### 0.6 修复复核（2026-11-19 10:04，实测）

§0.3 的 G2–G6 已由用户修复，`pytest tests/kb` 35 passed、`tests/core/test_hooks.py + tests/kb` 99 passed。

| 缺口 | 状态 | 落点 |
|---|---|---|
| G2 E2 通道 | **已修** | `runner.py:1738 _copy_kb_evidence`，在 `finally` 里容器销毁前调用（`:1315`），`BEN_KB_EVIDENCE` 已有人读；verify 侧把 `run_dir/.evidence/*` 合并进 `http_dir/`（`verify.py:208-215`） |
| G3 `stopped_code` | **已修** | 新增 `Ben/core/stopped.py`（`StoppedCode` + `coerce_stopped_code` + 遗留兜底），`main.py:418` 优先取 `result.stopped_code` |
| G4 两份重复映射 | **已修** | `_map_stopped_code` / `_stopped_code_from_reason` 均已删除（全仓 `grep` 为空） |
| G5 `or True` 死代码 | **已修** | `_norm_is_oracle` 已删除 |
| G6 硬编码靶场 | **已修** | 改读 `profile.verify_hint`；`local-scg-rce-positive.yaml` 已填；测试断言源码不再含该字符串 |

额外收严（值得保留的设计）：finding 里的 ` ```http ` 交换一律落 `findings_http/` 并标 **e3**，`confirm()` 明确拒绝 e3 ref，防止把报告里的粘贴当原始报文。

**仍存问题**：无（P1–P4 已接线；细节见 `tests/core/test_audit.py`）。

**已落地**：`Ben/core/audit.py`（AuditWriter/AuditHooks）、`CompositeHooks`、runner 接线、`ben transcript`、spill 双写 `audit/tools/`、verify `BEN_AUDIT=1` + `verdict.json.transcript` + 子原因 hint。

---

## 1. 目标与非目标

**目标**：一次 run 结束后，磁盘上能完整回答四个问题——

1. 模型收到了什么任务单（system prompt 原文，含注入了哪几份 skill）？
2. 它每一轮说了什么、调了什么工具、工具回了什么（完整，不截断）？
3. 大工具输出被截断时，原文在哪？
4. 它是在第几轮、看到哪句话之后走偏的？

**非目标**：

- 不改 SDK 内部实现，不接管模型调用，不包 `Model`。
- 不做云端上传、不做跨机汇总、不做 replay。
- 不把落盘做成扫描的前置条件；写失败只告警，不中断扫描。

---

## 2. 现状：数据在，但残缺

| 位置 | 有什么 | 缺陷（决定性） |
|---|---|---|
| `_artifacts/runs/<run>/.state/agents.db` | 每 agent 的会话条目（SQLite `agent_messages`） | 无 system prompt；**hooks 每轮临时注入的预算告警 / frontier 块不入库**（SDK 只持久化 `_original_input_for_persistence`，`agents/run_internal/run_loop.py:1635`）；会被 compaction `replace_session_items` 覆盖、图片被 elide |
| `_artifacts/reports/trace_*.json` | root 的工具调用 | 只 root、`output[:2000]` 截断（`runner.py:1689-1699`）；子 agent 报文不入库 |
| `Ben/llm/request_log.py`（`RequestLoggingModel`，991 行） | 每次 HTTP 尝试的状态码/耗时/token/成本/provider request-id | 头注释明确承诺 **「Raw request and response bodies are never captured」**——正文字节只量大小。这是隐私契约，不为调试破掉 |
| `Ben/tools/output_store.py` spill | 完整工具输出 → 沙箱 `/workspace/.tool-output/` | 容器销毁即丢失，宿主机无副本 |
| `render_system_prompt()`（`factory.py:1442`） | 最终 system prompt（skill 正文在此拼入） | 拼完即用，从不落盘；且它是**过滤前**的版本 |

**结论**：五个缺口 = 任务单无存档、注入消息不可见、工具输出只有缩略图、压缩会覆盖历史、无请求原件。现有三处「看起来像日志」的东西各有各的契约，谁都补不上这五个洞。

---

## 3. 小学生版：为什么要装录像机

- **现在的 verify 像考试只发成绩单**：告诉你 0 分还是 100 分、扣分原因是「没打到洞面」，但不告诉你孩子是没看见题、看错题、还是算错。
- **transcript 就是考场监控**：翻了几页书（注入了哪些 skill）、在草稿纸上写了什么（每轮 assistant 文本）、用了什么工具（发过哪些请求）、喇叭里广播了什么（预算告警、进度提示），全录下来。考完回放，一眼看出问题出在哪一轮。
- **录像要压缩，否则硬盘爆**：模型每轮都会拿到「到目前为止的全部对话」。每轮整本重抄，10 轮就是 10 份几乎一样的长文本。所以只抄**新加的那几页**，前面没变的记一句「同上次，共 N 页」。

一次调用、一条旁路、三种读法：

```
一次 LLM 调用
  ├─ on_llm_start  → 系统提示（变了才重存）+ 本轮全部输入的增量
  ├─ on_llm_end    → 它说了什么 + 要调哪个工具 + token
  ├─ on_tool_end   → 工具名、参数、返回（不截成 2000 字）
  └─ on_agent_*    → 谁派的活、给它的任务
                        │
                        ▼
        audit/events.jsonl   ← 唯一真相，一行一件事，随时 tail
                        │
        ┌───────────────┼──────────────────┐
        ▼               ▼                  ▼
   transcript.md   ben transcript      verdict.json
   （人读回放）     （按轮/按 agent 查）  （诊断多说一句为什么）
```

---

## 4. 架构：一个记录点

### 4.1 为什么是 SDK `RunHooks`

`Ben/core/hooks.py:180` 的 `ReportUsageHooks(RunHooks)` 已在用这套钩子做预算/轮次管理，证明它对**所有 agent（含子 agent）**生效：`runner.py:1039` 构造一次，`execution.py` 里所有 `Runner.run(..., hooks=hooks)` 共用同一实例。

| 候选挂点 | 能拿到 | 拿不到 | 结论 |
|---|---|---|---|
| `factory.build_agent` | 渲染好的 instructions、`resolved_skills` 清单 | 每轮输入、输出、工具结果 | 只够 A 项，且是过滤前版本 |
| `execution.run_agent_loop` 每轮结束 | 该轮新增 items | system prompt、**hooks 注入的告警**（注入发生在 `on_llm_start`，晚于循环取 items）、不覆盖子 agent | 会漏最关键的一类偏差来源 |
| 包 `Model`（`RequestLoggingModel`） | 完整 input + response，含重试 | ——但按 HTTP 尝试记，一次重试三条；且与遥测 sink 混在一起，会破隐私契约 | 不选 |
| **`RunHooks`** | `filtered.instructions`（**实际发给模型**的 system，过滤后）+ `filtered.input`（实际输入）+ `ModelResponse` + 工具结果 + agent 层级 | provider 原始 HTTP body（那是 E1/E2 的事） | **选它** |

关键一点：`run_loop.py:1623` 的 `hooks.on_llm_start(..., filtered.instructions, filtered.input)` 是**唯一的、所有路由都经过的**出口。挂在这里，等于挂在整个 agent 运行时的心脏上，而不必去改 factory 或主循环。

### 4.2 钩子 → 产物映射

| 钩子 | 记什么 | 答复哪一问 |
|---|---|---|
| `on_agent_start` | id / name / parent / model / 任务 | 谁派的活 |
| `on_llm_start` | system（`system_changed` 时才存正文，否则只存 sha256）+ 输入增量 | 第 1、4 问 |
| `on_llm_end` | `response.output`（message / function_call / reasoning）+ usage + finish_reason | 第 2、4 问 |
| `on_tool_start` / `on_tool_end` | 工具名、参数、**未截断**返回值、耗时 | 第 2、3 问 |
| `on_agent_end` | 最终输出、耗时 | 收尾 |
| `on_handoff` | from → to | 本项目基本不用，记一行待查（多 agent 走 coordinator，不走 handoff） |

### 4.3 目录布局

跟着 run 走，不新建顶层目录（`_artifacts/` 已 gitignore）：

```
_artifacts/runs/<run>/audit/
  events.jsonl             唯一真相（append-only，一行一事件，崩溃可读）
  index.json               run 元信息 + agent 表 + 统计（bytes/records/truncated/capped/secret_hits）
  transcript.md            全 agent 合并时间线（人读）
  agent-<name>-<id8>.md    单 agent 回放（人读，含子 agent）
  tools/<output_id>.txt    spill 出去的完整工具输出（C 项，容器存活期双写）
```

**不写进 `.state/`**：那里是 resume 的输入，混入新文件有风险。

### 4.4 `events.jsonl` 契约

```json
{"v":1,"seq":1,"ts":"2026-11-19T09:31:00.112+08:00","type":"run_start","run":"ben_20261119_0930_ab12cd34","target":"http://127.0.0.1:8080","mode":"delta"}
{"v":1,"seq":2,"ts":"...","type":"agent_start","agent":"a1b2c3","name":"Root Agent","parent":null,"task":"对 ... 做聚焦 IDOR 验证"}
{"v":1,"seq":9,"ts":"...","type":"llm_start","agent":"a1b2c3","turn":1,"model":"deepseek-chat",
 "system":{"sha256":"9f2c...","chars":9124},"system_changed":true,"system_text":"You are ...",
 "input":{"base":0,"total":2,"rewind":false,"items":[{"role":"user","content":"..."}]}}
{"v":1,"seq":10,"ts":"...","type":"llm_end","agent":"a1b2c3","turn":1,"duration_ms":4211,
 "usage":{"in":21033,"out":812,"cached":19000},"finish":"tool_calls",
 "output":[{"type":"message","text":"先把用户列表拉一遍"},
           {"type":"function_call","call_id":"c1","name":"http_request","arguments":"{\"method\":\"GET\",\"url\":\"...\"}"}]}
{"v":1,"seq":11,"ts":"...","type":"tool_end","agent":"a1b2c3","call_id":"c1","tool":"http_request","status":"ok","duration_ms":133,
 "result":{"sha256":"7a1e...","chars":2210,"text":"HTTP/1.1 200 OK\n..."}}
```

规则：

- `seq` 全局单调、跨 agent 有序（`asyncio.Lock` 串行分配 + 落盘），用于排序与断点续写。
- `input.base` = 与上一轮相比**未变的条目数**；重建第 N 轮输入 = 该 agent 之前所有 `items` 顺序拼接后取前 `total` 条。`rewind: true` 表示比上一轮短了（SDK `deduplicate_input_items_preferring_latest` 或 compaction 导致），此时从 `base` 起重发全量。
- `system_changed: false` 时省略 `system_text`。system 十几 KB 且几乎不变，每轮重存是自残。
- 单条 item 序列化 > 32KB（`BEN_AUDIT_ITEM_KB`）→ 存前 32KB + `{"truncated":true,"full_chars":N,"sha256":"..."}`。**静默省略等于把「没看到」伪装成「不存在」**，必须显式标注。

### 4.5 差分怎么做，以及为什么能砍掉「压缩前快照」

上一版方案要单独存压缩前快照，是因为它从 `agents.db` 取数据——而 `replace_session_items` 会把旧条目永久覆盖（`compaction.py:400`）。挂在 hooks 上之后这个需求自动消失：

**每轮的完整输入本来就被逐轮记下来了**，压缩前那一刻的历史 = 上一轮 `llm_start` 的可重建结果。压缩后的变化下一轮自然体现为 `rewind: true` + 新基线。所以 D 项从「额外写点」变成「已有数据的副产品」。

差分算法（逐条指纹求最长公共前缀）：

```
本轮 items → [(sha1(item), size), ...]
与上轮缓存逐条比对 → L = 第一个不同的位置
记 items[L:]，base=L，total=len(items)
```

- 正常单调追加：`L == 上轮长度`，只写新增几条 → 总体积 ≈ O(对话总长度)，不是 O(轮数 × 上下文)。
- 历史被改写（去重、压缩）：指纹对不上，`L` 变小，自动退化成「从 L 起重发」。**结果始终正确，只是那轮多写一些**。
- 最坏（每轮全变）退化成全量快照：仍然正确，只是费盘。宁可多写，不可写错。
- 需要逐字节复现时：`BEN_AUDIT_FULL=1` 强制每轮全量。

### 4.6 只剩一个额外写点：spill 双写（C 项）

spill 是唯一 hooks 覆盖不到的：`configure_spill_writer` 把完整输出写进沙箱 `/workspace/.tool-output/`，返回给模型的是「路径 + 预览」，容器一销毁就没了。所以 `Ben/tools/output_store.py:44` 的 writer 里加一行宿主机双写（`audit/tools/<id>.txt`），**必须在容器存活期写**。

---

## 5. 与 verify 的接线（这是「verify 要能做到的功能」的落点）

### 5.1 默认开 + verdict 字段

verify 起扫描前置 `BEN_AUDIT=1`，跑完在 `verdict.json` 增一段（只存指针，不复制产物，沿用 v2 的 D2 口径）：

```json
"transcript": {
  "dir": "_artifacts/runs/ben_20261119_0930_ab12cd34/audit",
  "events": 214, "agents": 3, "bytes": 1834220,
  "truncated_items": 2, "capped": false, "secret_hits": 4,
  "view": "ben transcript ben_20261119_0930_ab12cd34"
}
```

失败路径（预算中断、连接断开）同样要有——**越失败越需要它**，所以 `finally` 里也要 flush + render。

### 5.2 四个子原因各自多打印什么

| 子原因 | 现在只说 | 加上 transcript 后说 |
|---|---|---|
| `MISS_SURFACE` | agent 从未请求 oracle.path | 「本 run 共请求 7 个 path：`/`、`/api/users`、`/api/users/1`…；oracle 要 `/api/users/102`。第 6 轮它已拿到用户列表却没换 id。」→ 落 B 类「补侦察/入口」 |
| `TECHNIQUE_MISS` | 打到面但 4xx | 「第 4 轮用 `?id=102` 而非 `/102`，返回 404；oracle 是路径段形式。」→ 手法差在哪，一句话 |
| `FALSE_NEGATIVE` | 判据 FAIL | 「第 8 轮它自己写『该接口未见越权，放弃』，此时手上已有 200 响应但没对比属主字段。」→ 这才是「skill 缺哪一句」的证据 |
| `BLOCKED` | 有拦截特征 | 「拦截页出现在第 5 轮 `POST /api/users/102`，响应含 `Attention Required`（Cloudflare），`## Bypass Techniques` 现有 2 条都不适用。」 |

实现只读 `events.jsonl` 的**派生视图**（`paths_seen` / `turns_used` / `last_assistant_text` / `blocked_snippets`），**不做二次模型调用**——诊断必须便宜且确定。

### 5.3 证据等级：transcript 是 E0（诊断级）

v2 §5 的阶梯（E1 Caido 报文 / E2 沙箱自采 / E3 报告字段 / E4 trace）不变，前端加 **E0**：

| 级 | 来源 | 可信度 |
|---|---|---|
| **E0** | **audit/events.jsonl（对话落盘）** | **仅诊断**：回答「agent 看到/想过/调过什么」，**不得支撑 CONFIRMED** |

理由：transcript 里的 HTTP 是**工具返回值**，可能被工具层截断或加工；只有 E1/E2 的 `.req/.resp` 算原始报文。这条落成 layering 测试：`verdict.confirm()` 的 `Evidence` 不得来自 `Ben.core.audit`。否则会污染 v2 最硬的规则（D4：间接材料不得单独产生 0）。

---

## 6. 开关、权限、脱敏

| 变量 | 默认 | 作用 |
|---|---|---|
| `BEN_AUDIT` | `0`（普通扫描关）/ verify 默认 `1` | 总开关 |
| `BEN_AUDIT_FULL` | `0` | 1 = 每轮全量快照（放弃差分），用于逐字节复现 |
| `BEN_AUDIT_ITEM_KB` | `32` | 单条 item / 单次工具输出上限 |
| `BEN_AUDIT_MAX_MB` | `200` | 单 run 上限；超限写 `{"type":"capped"}` 后停记，扫描继续 |
| `BEN_AUDIT_REDACT` | `0` | 1 = **写前**脱敏（共享机器用）；默认原样落盘 |

- 目录 0700、文件 0600。
- **默认原样落盘**，与上一版「写前就涂」相反。理由：调试最高频的问题是「这个请求到底带了什么 header / cookie」，写前涂掉等于把最有用的证据毁掉。`_artifacts/` 已 gitignore、文件不对外。
- 脱敏只发生在**导出**：`ben transcript <run> --redact` 生成副本，规则沿用 v1 稿 §8——`Cookie:`/`Set-Cookie:` 整行、`Authorization:`/`Proxy-Authorization:`、`Bearer <token>`、`api_key|apikey|x-api-key` 键值、`.env` 里出现过的具体值；**保留前后各 4 字符**（`sk-1…c9f2`）便于判断「是不是同一个 key」。
- `index.json` 记 `secret_hits`，人读输出末行提示「含 N 处疑似凭据，对外分享请加 --redact」。
- 不注册到 `request_log` 的 sink 链：transcript 只写本地文件，绝不随遥测外发。

---

## 7. 失败隔离：录像机坏了不能把考场点着

1. 所有序列化、写入、渲染包在 `try/except Exception`：失败只 `logger.debug` + `index.json.errors += 1`，**永不向上抛**。
2. 上限触达 → 写 `capped` 后停记，扫描继续。
3. 写入 `await asyncio.to_thread(...)`（一次 append 一行），不阻塞事件循环；`seq` 分配与落盘由 `asyncio.Lock` 串行化。
4. `CompositeHooks` 逐个钩子 `try/except`：一个钩子抛错不影响另一个（否则 transcript 写失败会连带把预算管理搞挂，那才是真事故）。
5. layering：`Ben/core/audit.py` 不许 import `Ben/tools`、`Ben/report`、`Ben/interface`。

---

## 8. 代码改动清单

| 文件 | 动作 | 规模 |
|---|---|---|
| `Ben/core/audit.py` | **新增**：`AuditWriter`（seq/lock/上限/截断/指纹差分）、`AuditHooks(RunHooks)`、`load_events()`、`render_markdown()`、`redact()` | ~330 行 |
| `Ben/core/hooks.py` | **新增** `CompositeHooks`：7 个钩子逐个 fanout + 异常隔离 | ~60 行 |
| `Ben/core/runner.py` | ① `:1039` 构造 hooks 处按开关包 `CompositeHooks`；② 收尾调 `finalize()`；③ `AgentRunResult` 加 `audit_dir` | ~25 行 |
| `Ben/tools/output_store.py` | `configure_spill_writer` 的 writer 里加宿主机双写（C 项） | ~15 行 |
| `Ben/interface/cli/transcript.py` | **新增** `ben transcript <run> [--agent] [--turn] [--md] [--redact] [--json]` | ~120 行 |
| `Ben/interface/main.py` | 分发加 `transcript` 分支（与 `view` 同构） | ~5 行 |
| `Ben/interface/cli/completions.py` | `_ROOT_COMMANDS` 加 `transcript` | ~3 行 |
| `scripts/dump_transcript.py` | 保留为 `agents.db` 只读导出（P0，已完成）；读取逻辑收敛到 `Ben/core/audit.py` 后变薄壳 | 改 10 行 |
| `Ben/kb/verdict.py` + `report.py`（v2 实施后） | 子原因诊断接 E0 视图 | ~60 行 |
| `tests/core/test_audit.py` | 差分/回卷/截断/上限/异常隔离/零写盘 | ~260 行 |

**不改**：`execution.py`（主循环一行不动）、`request_log.py`（契约不动）、`agents/`、`factory.py`（v1 稿的 P1 挂点取消）。

---

## 9. 分期与验收

| 期 | 内容 | 验收（可自动断言） |
|---|---|---|
| **P0** | 只读导出 `agents.db`（**已完成**） | `scripts/dump_transcript.py <run>` 产出 `<name>__<id8>.jsonl` + `index.json` |
| **P1** | `AuditWriter` + `AuditHooks` + `CompositeHooks` + runner 接线 | ① 两轮单调追加：第二轮只写新增条目且 `base == 上轮 total`；② 人为删中间一条 → `base` 回退且重建结果正确；③ 单 item 40KB → 截断 32KB + `full_chars`；④ 触上限 → `capped` 且不抛；⑤ 假模型 2 轮 run 的事件序列完整（`run_start→agent_start→llm_start(system_changed=true)→llm_end→tool_start→tool_end→llm_start(system_changed=false)→…→run_end`）；⑥ **任一钩子故意抛错，run 仍正常完成** |
| **P2** | 渲染 + `ben transcript` | `transcript.md` 能回答「第 2 轮说了什么、调了什么、返回什么」；`--turn 2` 精确切轮；`--agent` 过滤子 agent；`--redact` 前后各留 4 字符 |
| **P3** | spill 双写（C 项） | 触发一次大输出，`audit/tools/<id>.txt` 字节数与原输出一致；容器销毁后仍可读 |
| **P4** | verify 接线 + `verdict.json.transcript` + 四类子原因诊断 | 假 runner 走 `MISS_SURFACE`：输出必须含 `paths_seen` 与「oracle 要 X，最接近 Y」的差集句 |

回归口径（本仓库既有约定）：

- `BEN_AUDIT=0` 时**零新增写盘**：monkeypatch `Path.write_text` / `open("w")` / `Path.mkdir`，断言 audit 模块全程零调用。
- 相关子集必须全绿：`tests/core/test_hooks.py`、`tests/core/test_budget_pause_policy.py`、`tests/core/test_e2e_budget_lifecycle.py`、`tests/core/test_audit.py`——这四个直接压在 `on_llm_start` 上，是本改动最易踩的回归面。
- 全量 pytest：本机约 99 个预先存在的失败为基线，改动后不得高于 99。

---

## 10. 不做

- replay（拿 transcript 重放一轮）；`ben view` 里挂时间轴（先留数据，不实现界面）
- 记录 provider 原始 HTTP body（要改 `request_log` 隐私契约，另案）
- 把落盘结果回灌给模型、自动归因（归因属 verify 的机械断言，不属日志）
- 改 SDK 源码、劫持 httpx
- 替代 `trace.json` / `agents.db`：三者定位不同，谁都不许删对方的写点

---

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| 体积失控（子 agent 多、工具返回大） | 指纹差分 + 单 item 32KB + 单 run 200MB + `capped` 优雅停记；P1 先用假模型量一次真实体量再定默认值 |
| 钩子抛错拖垮扫描 | `CompositeHooks` 逐钩子 `try/except` + writer 全包 + `to_thread` 不阻塞；P1 验收有专门一条 |
| 性能开销 | 每轮多一次 JSON 序列化（100KB 量级）+ 一次线程写，相对一次 LLM 调用可忽略；system 用 sha256 缓存，不重复序列化 |
| 泄漏凭据 | 目录 0700/0600、`_artifacts/` gitignored、`secret_hits` 计数、导出 `--redact`；默认开关是关的，只有 verify 与显式开关才开 |
| 与 v2 边界混淆 | E0 定位 + layering 测试禁止 `confirm()` 消费 transcript |
| 与 resume 冲突 | 不碰 `.state/`；resume 追加同一 run 的 `audit/`，`seq` 续写 |

---

## 12. 锁定项（D8–D10，与 `skill-verification-plan-v2.md` 一致）

| # | 事项 | 锁定取值 |
|---|---|---|
| D8 | 记录点 | SDK `RunHooks`（不包 Model、不改 `request_log`、不在 factory/主循环各挂一处） |
| D9 | 输入落盘方式 | 逐条指纹最长公共前缀差分；`BEN_AUDIT_FULL=1` 可强制全量 |
| D10 | transcript 的证据等级 | E0，诊断级，永不进 `confirm()` 的 `Evidence` |
| D11 | `CompositeHooks` 异常策略 | audit 写失败可吞；`BudgetExceededError` 等控制流异常必须原样上抛 |

---

## 13. 一句话验收

拿一次真实的 `MISS_SURFACE`，`ben transcript <run> --turn 6` 能把「它第 6 轮明明拿到了用户列表、却没换一个 id 去试」这件事**原样念给你听**——这条成立，方案就算成立。
