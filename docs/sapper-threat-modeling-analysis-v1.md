# Sapper 威胁建模实践分析 × Ben 借鉴评估 v1

> 对比对象：`/home/kali/Desktop/AI/sapper-v2`（commit 至 2026-11-14，`lib/sapper.py` 4,726 行）
> 参照对象：`/home/kali/Desktop/AI/BenProject`（Ben）
> 日期：2026-11-17
> 方法：静态阅读 Sapper 的 `lib/`、`templates/`、`SKILL.md`、`kb/`，配合全仓关键词检索；Ben 侧结论基于代码阅读与前一轮 20 个真实 run 的统计（`strix_runs/*/`）。
> 局限：未实际运行 `sapper dispatch`（需模型凭据），所有行为结论来自代码路径与模板文本，不来自运行观测。Sapper 仓库内 1.2GB 的 `sapper-v18-mixed.tar.gz` 未展开检索。

---

## 0. 结论摘要

**Sapper 没有威胁建模。** 在 `lib/ templates/ skill/ docs/ kb/ tests/` 及全部根级 markdown 中检索 `threat` / `STRIDE`，命中数为 **0**（排除 `.git`、图片资产、tar 包）。它也不存在"信任边界""攻击面""定级校准"这类构造。

但它并非对威胁毫无推理——它把这部分能力**拆散进了四个位置**，且都不是以"模型/文档"的形态存在：

| 威胁建模的环节 | Sapper 的实际载体 | 形态 |
|---|---|---|
| 威胁识别 | `templates/reason.md` 的攻击链 doctrine + 类型化事实图 + `kb/entries` 手法库 | 枚举式、技战术驱动 |
| 风险评估 | 仅 `templates/blindspot.md` 的三级 `risk` + `vuln` 事实的 `severity` 字符串 | 无标准、不参与排序 |
| 防护措施 | 面向 **agent 自身**的注入围栏、确认闸门、复核闸门 | 结构级、代码强制 |
| 防护建议（对目标） | 不存在（无报告生成器） | — |

| 判断 | 结论 |
|---|---|
| Sapper 是否值得在"威胁建模"维度上被借鉴 | **部分值得，但借鉴对象不是建模方法，而是四个结构模式** |
| Ben 的威胁建模是否有可被 Sapper 改进之处 | 有，且集中在**数据形态与生命周期**，不是分析深度 |
| Ben 是否应放弃自己的四段式模型去对齐 Sapper | **否。** Ben 的 `Severity Calibration` 正是 Sapper 缺失的能力 |

---

## 1. 证据与判定

| 检查项 | 结果 | 位置 |
|---|---|---|
| `threat` / `STRIDE` 全仓检索 | 0 命中 | 全仓（除 `.git`、assets、tar） |
| 事实类型枚举 | `host / service / cred / vuln / loot / flag / note` | `lib/sapper.py:51` |
| 是否存在独立的威胁/边界/假设节点类型 | 无 | 同上 |
| `severity` 出现处 | 仅作为 `vuln` 事实的 `data` 键名 | `templates/explore.md:53` |
| `risk` 出现处 | 仅 `blindspot` 输出的三级枚举 | `templates/blindspot.md:21`、`lib/sapper.py:3067-3069` |
| 项目自我定位 | "engagement fact graph"、"offensive specialization of fgc" | `SKILL.md:1-16` |
| 报告产出 | 无。`sapper view` 出的是图的可视化 HTML | `SKILL.md`「view — the HTML visualization」 |

因此下文的"实现方式"必须读作：**Sapper 用什么替代了威胁建模**，以及这种替代在三个维度上分别走到了哪一步。

---

## 2. 威胁识别

### 2.1 载体一：类型化事实 + 意图 DAG —— 攻击树的等价物

Sapper 的核心数据结构天然是一棵攻击树，只是它不叫这个名字：

- **事实（fact）** 是不可变的观测节点（`sapper.py:585` 注释 "Facts are immutable by convention"）。
- **意图（intent）** 是超边：`from` = 依赖的事实列表，`to` = 它产出的事实（`sapper.py:322-351`）。
- **就绪判定**：`is_ready` = 未完成 ∧ `from` 全部存在 ∧ 未处于待确认态（`sapper.py:833-847`）。

这套结构对攻击树的表达力实际比文档更强：

- **AND 分支**由 `from` 列表直接表达（一条意图依赖 f001+f003+f007）。
- **OR 分支**由多条意图共享同一组 `from` 表达——但 Sapper 不做"选哪条"的推理，`frontier` 把它们全部列为就绪，谁先被 `pick` 谁先做（`sapper.py:938-949`）。
- **深度优先**由 `pick` 的 FIFO + 依赖满足隐式决定，没有显式策略。

**关键缺口**：`FACT_KINDS` 里没有"威胁"或"假设"类型。识别过程的中间产物（"可能存在 SSRF，因为这里做了 URL 转发"）**无处安放**——它只能要么变成一条 `vuln` 事实（要求已验证，见 `explore.md:99` "A scanner WARNING is not a vulnerability"），要么变成一条 `note`，而 `note` 不参与任何推理或排序。这正是威胁建模与漏洞验证的分界线：Sapper 只允许前者落地。

### 2.2 载体二：reason.md 的攻击链 doctrine

`templates/reason.md:65-96` 是 Sapper 唯一的"全局威胁视角"，但它是一串**动作纪律**而非威胁模型：

```
recon → service/version enumeration → vulnerability identification →
exploitation → credential & loot harvest → privilege escalation →
(if multi-host) pivot/lateral → flag
```

配套三条策略（`reason.md:69-80`）：

1. **只提最多 3 条意图**（`max_intents`，`sapper.py:1186-1197` 硬编码 `"3"`）；
2. **cheap wins first**——理由是分数机制（"Score locks at first solve"），不是风险；
3. **凭据复用 / 同产品同 CVE 横向关联**——这是 Sapper 最接近"威胁推理"的部分：它要求把 `cred` 事实关联到 SSH/RDP/DB/后台，把 `service` 指纹关联到 CVE。

外加一条**去重纪律**（`reason.md:91-96`）：提新意图前必须 `sapper recall` 查历史节点。这是"防重复劳动"，不是"防遗漏威胁"。

### 2.3 载体三：探索 / 发散 / 自审 / 复核四件套

| 模板 | 作用 | 是否有威胁语义 |
|---|---|---|
| `brainstorm.md` | 生成 3-4 条**互斥策略**供人选择，含 `effort: low/medium/high` | 只比"代价"，不比"风险" |
| `blindspot.md` | 自审：找出图上**缺失**的假设、风险、知识空白，3-7 条，带 `risk` 三级 | **唯一显式使用"风险"的地方** |
| `verify.md` | 独立复核执行者声称的结果，只跑最小只读复现 | 复核"事实是否为真"，不复核"边界是否为真" |
| `explore.md` | 执行单条意图，要求填 `deviations`（计划 vs 实际） | 偏离记录是事后信息，不回写模型 |

`blindspot.md:29-35` 的规则值得单独摘出，它是 Sapper 最接近威胁建模的一段文字：

> Identify 3-7 blindspots, prioritized by risk. ... Focus on what is MISSING from the graph, not what is already there. ... Consider: missing validations, untested assumptions, integration risks, edge cases, performance concerns, security gaps, dependency risks.

注意它的四个特征：**数量受限（3-7）**、**必须可行动**、**面向缺失而非重复已知**、**按 risk 排序**。这套约束设计比"写一份威胁模型"更不容易产空话。

### 2.4 载体四：KB 与 hook

- `kb/entries/`（20+ 条）是手法库：`sapper kb search "thinkphp rce"`、`kb show <id>` 取完整命令与思路。`explore.md:42-47` 要求执行前先查 KB。这解决"知道该试什么"，不解决"该不该试"。
- `lib/sapper-hook.py:1-24`：项目内存在 `.sapper/` 时，每轮 `UserPromptSubmit` 注入当前 frontier + goal。**威胁上下文是常驻推送的，不是按需拉取的。**

### 2.5 对识别环节的评价

**强项**：识别结果可直接执行。每条 intent 都要求写明具体命令、期望证据、时间盒（`reason.md:81-86`），不存在"模型写完没人用"的典型失效。事实不可变 + 依赖显式，使得"我们验证到哪一步"永远可查。

**弱项**：**枚举式识别覆盖不到没有 CVE 的攻击面。** 攻击链 doctrine 的每一环都对应"有工具、有 payload"的动作；而 IDOR、业务逻辑越权、租户隔离缺失、认证前后权限差这类问题，其识别前提是"先知道这个系统的角色与边界"——Sapper 既没有角色/租户的表达能力（`FACT_KINDS` 无 `role` / `tenant`），也没有地方写"这个接口本应只有管理员可达"。`blindspot` 偶尔会碰到（"security gaps"），但它输出到 hint，不进结构化台账，也不阻塞任何流程。

---

## 3. 风险评估

### 3.1 唯一的正式风险等级：blindspot.risk

`templates/blindspot.md:21` 定义 `risk: high|medium|low`，`lib/sapper.py:3049-3080` 落库时做枚举校验：

```python
risk = str(spot.get("risk", "medium")).strip().lower() or "medium"
if risk not in {"high", "medium", "low"}:
    risk = "medium"
content = f"[blindspot/{risk}] {area}: {question}"
store.add_hint(content=content, creator="blindspot")
```

三个观察：

1. **等级是离散且代码可校验的**——非法值降级为 `medium`，不会污染数据。这是良好的工程习惯（相比自由文本"高/中/低"）。
2. **落点是 hint 而不是结构化风险台账**——`add_hint`（`sapper.py:566`）写入的是自由文本通道。等级被编码在字符串前缀 `[blindspot/high]` 里，**没有任何代码会解析它**。
3. **不参与排序**：`frontier`（`sapper.py:938-949`）的排序键是 `(status != "claimed", created_at)`，即"已认领的优先展示，其余按创建时间"。`risk` 与调度完全无关。

### 3.2 vuln 事实的 severity 是自由字符串

`explore.md:53` 声明 `vuln` 事实的 data 键含 `severity`，但 `normalize_fact_data`（`sapper.py:69-89`）只做"值转字符串 + 键名规范化"，**没有枚举、没有必填、没有校验**。`sapper validate`（`sapper.py:868-937`）校验的是图完整性（依赖存在、`to`/`source_intent` 互指、goal 边唯一），与定级无关。

结果是：同一个模型在同一次交战里可能写出 `severity: high` 与 `severity: 高` 与 `severity: Critical (CVSS 9.1)` 三种形态，且无人能据此排序或汇总。

### 3.3 排序依据：FIFO，无风险权重

```python
ready.sort(key=lambda i: (i["status"] != "claimed", i["created_at"]))
```

`pick`（`sapper.py:1639-1665`）取 `pool[0]`。`brainstorm` 的 `effort: low/medium/high` 也只存在于候选项的 doc 里供人看，不进调度。

### 3.4 经济激励不是安全风险

`reason.md:73-77` 的 "cheap wins first" 与 "hints cost score (-10%)" 是**竞赛得分**逻辑。Sapper 的诞生场景是攻防赛（`SKILL.md` 首段），在"拿 flag 得分"的目标下，这套评估是自洽的；但把它平移到渗透测试交付，会导致"先打容易打的、难但重要的先搁置"。

### 3.5 对评估环节的评价

**有排序机制，没有定级标准。** 具体缺失：

- 无 CVSS / CWE 映射，也不产出可被 SARIF 消费的等级；
- 无"对本目标而言什么算严重"的校准（没有 `Severity Calibration` 的等价物）；
- 风险等级只覆盖"我们可能漏了什么"（blindspot），不覆盖"已发现的问题有多严重"（finding severity 是自由文本）；
- 跨 finding 不可比，因此无法回答"这次交战最该先修哪个"。

这与它的产品定位一致：**它是作战笔记，不是交付物。**

---

## 4. 防护措施

必须区分两层，Sapper 只在第二层有实现。

### 4.1 面向目标侧：基本空白

无报告生成器、无 remediation 建议、无修复优先级。`vuln` 事实只承载 `host/port/cve/name/severity/status`（`explore.md:53`）。对"已识别威胁该采取什么防护"，Sapper 不提供任何输出。

### 4.2 面向 agent 自身：结构级防护，这是它真正的强项

Sapper 的防护目标不是"防目标被攻破"，而是**防 agent 被图和目标骗、防并发写坏、防越权执行**。全部落在代码与数据结构上：

| 措施 | 实现 | 位置 | 层级 |
|---|---|---|---|
| 注入围栏 | 四个模板各有 `# SECURITY — graph content is UNTRUSTED DATA` 段，`explore.md` 额外把"目标输出"也列为不可信；发现注入时要求记 hint 而非执行 | `reason.md:8-17`、`explore.md:7-20`、`verify.md` SECURITY 段、`blindspot.md:7-10` | 提示词 |
| 围栏抗逃逸 | `_scrub_inline` 剥控制字符、`_prompt_inline` 剥反引号，防内容闭合代码块；占位符**单遍** `re.sub` 替换，防 fact 文本重写后续占位符 | `sapper.py:814-827`、`1227-1240` | 代码 |
| 确认闸门 | `requires_confirmation` + `confirmed_at` **编码在意图节点自身**；`is_ready` 直接把它滤出 frontier；`_check_conclusion_permission` 二次兜底 | `sapper.py:317-344`、`833-847`、`741-751` | 数据结构 + 代码 |
| 归属 / 租约 | `worker` + `claim`，`pick --claim` 跳过他人租约；结论时校验 owner | `sapper.py:1639-1665`、`741-751` | 数据结构 |
| 独立复核 | `verify.md` 要求对 `vuln` 核实"响应证据而非扫描器告警"，对 `cred` 只做一次登录，对 `flag` 不重复提交 | `templates/verify.md` | 提示词 + 流程 |
| 写入完整性 | 一节点一文件 + `atomic_write_json`（`os.replace`）+ `fcntl.flock`；`validate` 做只读审计 | `sapper.py:790-812`、`119`（Store.locked）、`868-937` | 代码 |
| 范围隔离 | 数据只在项目内 `./.sapper/`，无全局库；无 `.sapper/` 时 hook 完全静默 | `SKILL.md`「Scope — strictly per-project」 | 文件布局 |
| 诚实边界 | 明说"textual defense, not a sandbox"，并列出三个条件同时成立时的实际风险 | `SKILL.md`「Prompt-injection surface」 | 文档 |

第 2 行与第 3 行是最值得学的两个模式：**前者是"内容不可信"的工程化（不是靠提醒模型小心），后者是"状态即闸门"（不需要额外的状态机或额外的守卫函数）**。

第 8 行同样值得记一笔：Sapper 明确承认文本防护挡不住注入，并把危险组合（不可信图 + `--skip-permissions` + 真实 shell）写进文档而不是假装安全。这是工程诚实度，不是能力。

---

## 5. Ben × Sapper 对照

| 维度 | Sapper | Ben | 谁更强 |
|---|---|---|---|
| 威胁模型产物 | 无 | `Ben/tools/threat_model/tools.py`，四段式 Markdown，run 作用域 | **Ben** |
| 定级标准 | 无（`severity` 自由字符串） | `Severity Calibration` 段，要求给出 critical/high/medium/low 的具体例子（`tools.py:570-573`） | **Ben** |
| 边界表达 | 无（`FACT_KINDS` 无 role/tenant） | 自由文本「Trust Boundaries and Assumptions」 | **Ben**，但仅文本层 |
| 结构可校验性 | `blindspot.risk` 枚举强制；`requires_confirmation` 布尔校验 | 仅校验"四段标题存在 + 长度"，正文语义不可校验 | **Sapper** |
| 修订/演进 | 事实不可变，派生即新节点（天然留版本） | `save` 覆盖全文并**清空** amendments（`tools.py:364-389`） | **Sapper** |
| 上下文注入 | title+description 进 prompt，`doc` 按需取（`sapper.py:1151-1185` 只标 📄） | `get` 返回全文（实测 3.2k 字符/次） | **Sapper** |
| 对手审查 | `blindspot` 角色化自审，3-7 条带 risk | 无独立机制，只有子 agent 被动 `amend` | **Sapper** |
| 授权强制 | 提示词 + 确认闸门（数据级） | `factory.py` 代码拒绝调用（`CLAUDE.md` 不变量 2） | **Ben** |
| 调度排序 | FIFO | `task_graph` + claims（已有更复杂机制） | 平手，均非风险加权 |

---

## 6. Ben 可借鉴的五个模式

### 6.1 三层文档纪律 → 威胁模型应按需分层返回

**Sapper 做法**：`render_graph_text`（`sapper.py:1151-1185`）只序列化 `title` + `description`，`doc` 仅用 `📄` 标记存在性，模型需要时自己 `sapper doc <id>`。上限是结构性的而非截断性的。

**Ben 现状**：`_get_impl`（`tools.py:296-324`）把 `content` 全文返回。上一轮实测：3 个真实模型 2157/3457/4055 字符，平均 3.2k，每个读取者会话一次性注入约 1.1k tokens 并在后续每轮重复计费。

**改法**：`get_threat_model` 增加 `detail: Literal["summary","full"] = "summary"`。summary 返回"信任边界 + 定级校准"两段的压缩版（目标 400-600 字符），full 走现有路径。这与项目内 `docs/dfs-on-demand-design-v1.md` 的 D1+D2 决策同向，不是新发明。

**预期**：单次读取从 ~1.1k tokens 降到 ~200，且不损失"是谁、边界在哪、什么算严重"这三个必需答案——因为 `Attack Surface` 段只在具体作业时才需要细节。

### 6.2 事实不可变 + 派生新节点 → save 不应销毁修订

**Sapper 做法**：事实一旦写入不可变（`sapper.py:585`），新认知 = 新节点 + 新边，旧节点保留。

**Ben 现状**：`_save_impl` 整体替换 `_MODELS[identity]`，并把 `amendments` 清空，只在返回消息里提醒"make sure what they said survives in the text you just wrote"（`tools.py:379-383`）。**这是提示词级别的保障，不是结构级别的保障**——root 一次二次 `save` 若漏抄，其他 agent 的修订就永久消失，且无人能发现丢了什么。

**改法**：`save` 时把旧 `{content, amendments, written_at, written_by}` 压入 `revisions[]`（保留最近 3 版），`amendments_cleared` 改为 `archived_as: "rev-N"`。这样"清空"变成"归档"，与 `AMENDMENTS` 上限（40 条）共同构成有界历史。

### 6.3 状态编码进结构 → `verified` 应从提示词走进字段

**Sapper 做法**：`requires_confirmation` 不是一句"请谨慎"的提示，而是节点上的布尔字段；`is_ready` 直接把它滤掉，`_check_conclusion_permission` 再兜底。状态在数据里，所以无法被模型忽略。

**Ben 现状**：`save_threat_model` 的 docstring 只说"say which parts are inferred rather than observed"（`tools.py:539-541`）。这是**纯提示词约束**，没有任何字段承载，也没有任何代码能区分"我验证过这个边界"与"我猜的"。后果具体：`get` 的消费者无法判断一条边界该不该当作前提使用。

**改法**：模型增加机器可读的 `boundaries: [{name, actors, verified: "inferred"|"observed"|"disproven"}]`，`save` 时校验枚举（照搬 `sapper.py:3067-3069` 的降级策略：非法值归 `inferred`），`get` 按 `verified` 分组返回。这样"边界是推断的"从建议变成可查询事实，report 侧也能据此降低定级置信度。

### 6.4 盲区审查角色化 → 威胁模型需要一个对手

**Sapper 做法**：`blindspot.md` 是独立的一次 dispatch，目标明确是"找图上缺什么"，输出 3-7 条带 `risk` 的质疑，且**禁止提意图**（"Do not propose intents. That is the job of dispatch reason"，`blindspot.md:35`）。这种职责隔离让它可以专门唱反调。

**Ben 现状**：`amend_threat_model` 是**被动**通道——要等子 agent 在实际作业中撞到矛盾才修。没有人专门问"这份模型的假设里哪条最可能错"。`bench`/`validate` 这类角色只做复核，不做质疑。

**改法**：新增 `review_threat_model(target)`，或让 validator/`general` 档子 agent 承担一次 blindspot pass，输出格式对齐 Sapper：

```json
{"blindspots": [{"area": "...", "risk": "high|medium|low", "question": "...", "suggestion": "..."}]}
```

落点直接写进 `amendments`（带 `by: "blindspot-review"`），从而复用现有的读取路径，不新增台账。

### 6.5 结论权限 → 基线只该由 root 写

**Sapper 现状**：`worker` + `claim` 保证一条意图只有一个执行者；结论时 `_check_conclusion_permission` 校验归属。

**Ben 现状**：三个工具在 `_BASE_TOOLS` 里对全量 agent 开放（`factory.py:1047-1092`），任何子 agent 都能 `save` 覆盖基线与全部修订。当前只靠 `system_prompt.jinja:184-185` 的"基线由主 Agent save"这句自然语言约束。

**改法**：`_save_impl` 增加调用者校验——`ctx.context["parent_id"] is None`（root）才允许 `save`，子 agent 只能 `amend`。错误信息直接指路 `amend_threat_model`。这是把 Ben 已有的"授权在代码里"不变量（`CLAUDE.md`）延伸到威胁模型。

---

## 7. Ben 不应照抄的四点

1. **不要放弃 `Severity Calibration`。** Sapper 的 `severity` 是自由字符串，跨 finding 不可比；Ben 的四段式里这一段是唯一能支撑交付报告定级的锚点。删掉它去对齐 Sapper，等于用"更结构化"换"更不可比"。
2. **不要采用 FIFO 调度。** Sapper 的 `frontier` 排序与风险无关，这在"拿 flag 得分"的竞赛里合理，在"先修最危险的"的交付里是错的。Ben 的 `task_graph` 若要加权，应取 `Severity Calibration` 而不是创建时间。
3. **不要把风险等级存成字符串前缀。** Sapper 把 `risk` 编进 `[blindspot/high] {area}: {question}` 的 hint 文本里，此后无人解析。若 Ben 引入风险等级，应为独立字段。
4. **不要退化成纯文本防护。** Sapper 自己承认注入围栏挡不住（`SKILL.md`），Ben 的 `factory.py` 是代码级拒绝。两者不在同一层，不能为了"像 Sapper 一样轻"而拆掉代码强制。

---

## 8. 改进清单

| 优先级 | 改动 | 主要文件 | 验收标准 |
|---|---|---|---|
| P0 | `get_threat_model` 增加 `detail` 参数，默认返回 summary | `Ben/tools/threat_model/tools.py:296-324`、`480-517` | 新增用例：summary ≤ 800 字符且含边界与定级要点；full 行为不变 |
| P0 | `save` 归档旧版到 `revisions[]`（保留 3 版），不再静默清空 | `tools.py:327-389` | 用例：二次 save 后 `revisions` 长度 +1，原 amendments 可读 |
| P1 | 模型增加 `boundaries[].verified` 枚举，`save` 校验 + 非法降级 | `tools.py:327-389`、`531-573` docstring | 用例：非法 `verified` 归 `inferred`；`get` 按状态分组 |
| P1 | 新增 blindspot 审查（工具或角色化子 agent），结果写 `amendments` | `tools.py`（新工具）+ `Ben/agents/profiles.py` | 用例：输出 3-7 条、`risk` 枚举、落库为带署名的 amendment |
| P2 | `save` 限定 root 调用，子 agent 只能 `amend` | `tools.py:327-389`、`Ben/agents/factory.py` | 用例：子 agent 调 `save` 返回指向 `amend` 的错误 |
| P2 | 压缩三个工具的 docstring（实测 2041 tokens/请求），结构约束移入 skill 按需加载 | `tools.py:480-638` | 测量：`_BASE_TOOLS` 中三项描述字符数下降 ≥40% |
| P2 | 补 `recon` / `enum` profile 缺失的 `get_threat_model` | `Ben/agents/profiles.py:81-145` | 用例：两个 profile 的工具面含 threat 工具 |
| P2 | 打通 `Severity Calibration` 与 SARIF 等级（当前 `stride:` 标签与模型无关联） | `Ben/report/sarif.py:104-130` | 用例：模型校准后的等级影响 `_SEVERITY_TO_SCORE` 取值 |

**回归判定**：本机全量 pytest 有约 99 个预先存在的环境失败，判定改动是否引入回归时以该基线比较，或只跑 `tests/tools/test_threat_model_tool.py`（29 个用例）。

---

## 9. 附：一个反直觉的观察

Sapper 在"威胁建模"上是空白，却在"威胁建模的失效模式"上做得比 Ben 好。

Ben 的四段式模型最可能的失效是：**root 在信息最少时写下一份看似完整的文档，此后无人挑战它，而它被所有子 agent 当作前提。** 上一轮统计显示：20 个 run 中只有 3 个建过模型，修订总数为 0——意味着这份文档在实践中既没被充分质疑，也没被持续校正。

Sapper 恰好对这两个点各有一个结构性答案：`blindspot`（专门挑战假设）与事实不可变（认知只能追加，不能覆盖）。这两个机制与"有没有威胁模型"无关，可以独立移植。**Ben 该学的不是 Sapper 如何建模，而是 Sapper 如何防止一份共享文档腐烂。**
