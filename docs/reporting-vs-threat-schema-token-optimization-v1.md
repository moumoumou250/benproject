# 报告工具 vs 威胁建模工具的 Schema Token 优化方案

> 测量对象：`Ben/tools/reporting/tool.py`（2,428 行，6 个工具）与 `Ben/tools/threat_model/tools.py`（3 个工具）
> 对照对象：`/home/kali/Desktop/AI/sapper-v2`（CLI + fact graph，无 function schema）
> 口径：从源码签名与 docstring 真实构建 `FunctionTool`，序列化为 `{name, description, parameters}` 后按 **3.9 字符/token** 折算（英文技术文本标定值，与 9k / 1.6k 两个已知锚点吻合）。未使用 tiktoken——离线环境无法下载词表；换算误差约 ±5%。

---

## 一、实测基线

### 1.1 两套工具的绝对体量

| 工具 | schema 字符 | ≈ token | 其中描述 | 参数说明 | 结构开销 |
|---|---:|---:|---:|---:|---:|
| `create_vulnerability_report` | 13,767 | 3,530 | 2,805 | 8,891 | 2,071 |
| `create_dependency_report` | 11,242 | 2,883 | 2,345 | 7,551 | 1,346 |
| `update_vulnerability_report` | 6,796 | 1,742 | 2,004 | 4,202 | 590 |
| `list_reports` | 2,728 | 700 | 1,636 | 561 | 531 |
| `delete_vulnerability_report` | 1,559 | 400 | 1,079 | 276 | 204 |
| `get_report` | 703 | 180 | 342 | 121 | 240 |
| **报告合计** | **36,795** | **9,435** | 10,211 (28%) | 15,245 (41%) | 11,339 (31%) |
| `save_threat_model` | 3,404 | 873 | 2,849 | 388 | 167 |
| `amend_threat_model` | 1,917 | 492 | 1,351 | 419 | 147 |
| `get_threat_model` | 1,573 | 403 | 1,128 | 299 | 146 |
| **威胁建模合计** | **6,894** | **1,768** | 5,328 (77%) | 611 (9%) | 955 (14%) |

两者的形态完全不同：报告工具的 41% 花在**字段说明**上（24 个具名参数，逐个解释），威胁建模的 77% 花在**工具描述**上（只有 2 个参数，说明却写成了方法论）。

### 1.2 它占多少

导入 `_BASE_TOOLS`（45 个工具）实测：全量工具面 **91,693 字符 ≈ 23,511 token**，其中

- 报告类 36,902 字符 ≈ **9,462 token，占 40%**；
- 威胁建模 6,987 字符 ≈ **1,792 token，占 7.6%**。

真实运行的 `run.json` 显示每请求输入 36k–38k token（`972f`、`4142` 两个 run，缓存命中 57%–74%）。也就是说**每个请求的静态前缀里约 65% 是工具 schema**。

### 1.3 谁在为这 11.2k 付费

| Agent | 报告工具 | 威胁建模 | 说明 |
|---|---|---|---|
| root | 880 tok（`list_reports`+`get_report`） | 1,768 tok | `_ROOT_REPORT_WRITE_DROP`（`factory.py:1116-1122`）已裁掉 4 个写工具，注释写明"历史上可占工具 schema ~40%+" |
| `recon` / `enum` | **0** | **0** | 白名单固定（`profiles.py:81-145`），两类工具都不在列 |
| `exploit` / `validate` / `default` | 9,435 tok | 1,768 tok | `tool_names=None` → 全量 `_BASE_TOOLS` |

结论：**11.2k 的账单由执行类子 Agent 承担**，而它们恰恰是轮次最紧、最需要把上下文留给目标细节的那一批。root 早已减过一次负，子 Agent 没有。

---

## 二、与 Sapper 的结构性差异

Sapper 没有 function schema。它的"工具面"是一个 CLI（`sapper fact|intent|done|...`，30+ 子命令，`--help` 全文 3,519 字符），字段词汇是一个 `--kind` 枚举（`host/service/cred/vuln/loot/flag/note`，7 值）加自由 `-d key=value`。

| 维度 | Ben 报告工具 | Ben 威胁建模 | Sapper |
|---|---|---|---|
| 常驻成本 | 9,435 tok，每请求每 Agent | 1,768 tok | **0**（`--help` 不调用就不进上下文） |
| 完整文档在哪 | 内联在 schema | 内联在 schema | `SKILL.md` 15,615 字符，**按需加载** |
| 字段粒度 | 24 个具名参数，逐个带 100–3,400 字符说明 | 2 个参数 + 4 个必填段落（靠运行时校验） | 1 个 `kind` 枚举 + 自由 `key=value` |
| 约束落在哪 | 写在描述里，读不到就写错 | 常量校验（400 字符、4 段标题） | `validate_graph()` 写后校验，错误在执行期返回 |
| 单一词汇 | 同一套字段在 create / update / dependency 里**重复描述三遍** | 单一模型，无重复 | 一套 `fact`/`intent` 词汇，所有子命令共用 |
| 每轮注入 | 无 | 无 | frontier 渲染（facts + intents + hints），是**状态**而非规则 |

关键差别不是"谁写得少"，而是**静态规则和动态状态谁该常驻**。Sapper 把规则放到按需加载的 skill 里，把常驻预算留给状态（它必须常驻，否则模型每轮要重新查询）；Ben 反过来了——规则常驻、状态按需查询。

Ben 已经有这个机制：`skills/analysis/` 下有 `reporting_discipline.md`（3,780 字符）、`counterevidence.md`（9,265）、`fix_verification.md`（6,003）、`severity_calibration.md`（5,620），并且 `reporting_discipline.md:16` 的小标题直接写着"**原 create_vulnerability_report 长 description**"。**迁移做过一次，但没有删源**——所以现在是两份付费：skill 里一份，schema 里一份。

---

## 三、四处具体冗余（带行号）

### 3.1 字段文档错位：`code_locations` 的说明挂在了 `http_exchange_ids` 下

`tool.py:1126-1199`，`http_exchange_ids` 的 Args 条目里，从"**How `fix_before` / `fix_after` work**"开始，到"Common mistakes to avoid"结束，共 **2,697 字符**（占该字段 3,408 字符的 79%）全部是 `code_locations` 的字段规范（`file` 相对路径、`start_line` 1-based、多段修改要拆条、`fix_before` 必须逐字符一致……）。

后果有两层：

- **成本**：`http_exchange_ids` 的描述被撑到 3,408 字符（约 874 tok），是全套工具里最贵的单个字段；
- **正确性**：`code_locations` 自己的描述只剩 46 字符（"White-box findings — list of location objects."）。模型读 schema 时，`code_locations` 的约束是空的，而规则出现在一个**它不会去读的字段**下面。白盒场景下 `start_line` 猜错、`fix_before` 改写格式，都会从这里漏出去。

这一项**不省 token 也要修**——把 2,697 字符搬回 `code_locations`（压缩到约 600 字符）是净 +140 tok 的"亏本"改动，但它是唯一一处会直接导致报告质量下降的缺陷。

### 3.2 `fix_verification` 的 1,604 字符，运行时错误信息里已经完整重复

`tool.py:335-357` 的 `_validate_fix_verification` 在缺字段时返回的报错文本，逐条复述了 4 道闸门（security closure / bypass review / preserved behavior / 如何验证）。也就是说：**模型不读描述也不会漏规则，一旦违规，报错会把规则原样送给它。**

而参数描述里那 1,604 字符（约 411 tok）是同一套话的散文版。这是典型的"schema 与 validator 双写"。

### 3.3 工具描述与已有 skill 文档重复

| schema 里的段落 | 字符 | 已存在于 |
|---|---:|---|
| create 的 "Report output rules"（禁内部路径 / 语气 / markdown / 字段分工） | ~700 | `reporting_discipline.md:16` 起，几乎逐条对应 |
| create 的 "Counterevidence pass" | ~600 | `counterevidence.md:152` "Before You File a Report" |
| create 的 "Nice to have: git blame" | ~290 | 属可选项，无强约束 |
| dep 的 reachability 五级阶梯 | ~1,000 | `dependency_cve_scanning` skill + `reachability` 参数本身 |
| `contextual_cvss_breakdown` 的 8 指标展开 | ~1,126 | 与 create 的 `cvss_breakdown` 说明**同一内容第三遍** |

### 3.4 共享 schema 里放角色指令

`list_reports` 的描述 1,636 字符，开头两段是"**For the orchestrator / root agent**……If you are a subagent, ignore it and focus on your task."——这段是写给 root 的，却对每个子 Agent 都计费，而子 Agent 恰好不该调它。角色分工属于提示词，不属于共享 schema。

---

## 四、优化清单

### P0：零风险，先做（净减 6,475 字符 ≈ 1,660 tok）

| # | 位置 | 现状 | 改法 | Δ字符 | Δtoken | 对正确性的影响 |
|---|---|---|---:|---:|---:|---|
| 1 | `tool.py:1126-1139` `http_exchange_ids` 描述 | 3,408 | 只留 ID 规则（约 304 字符） | −3,104 | −796 | **正面**：长文搬走，字段职责变清晰 |
| 2 | `tool.py:1139-1199` → `code_locations` | 46 | 接收压缩后的 591 字符规范 | +545 | +140 | **强正面**：修掉错位，白盒字段第一次有约束 |
| 3 | `tool.py:1200-1250` `fix_verification` | 1,604 | 258 字符 + 指向 `analysis/fix_verification` | −1,346 | −345 | 中性：违规时报错全文回放 |
| 4 | create 描述删 "Report output rules" | 2,805 | 727（保留 when/when-not/severity gate/dedup + skill 指针） | −2,078 | −533 | 中性偏正：规则在 skill 里，且首次上报前本就要 `load_skill` |
| 5 | `counterevidence` + `confidence` | 485 + 377 | 196 + 174 | −492 | −126 | 中性：`counterevidence.md` 覆盖 |

### P1：结构性瘦身（净减 7,519 字符 ≈ 1,928 tok）

| # | 位置 | 现状 | 改法 | Δ字符 | Δtoken | 影响 |
|---|---|---|---:|---:|---:|---|
| 6 | `create_dependency_report` 描述 | 2,345 | 790（reachability 阶梯交给参数与 skill） | −1,555 | −399 | 中性：阶梯在 `reachability` 参数里仍有枚举 |
| 7 | `update_vulnerability_report` 描述 | 2,004 | 700（保留 4 条字段交互规则，删使用场景散文） | −1,304 | −334 | 中性 |
| 8 | `list_reports` 描述 | 1,636 | 526（删"你不是 root 别调"；保留返回结构与过滤语义） | −1,110 | −285 | 中性：角色分工应进 root 提示词 |
| 9 | `contextual_cvss_breakdown` | 1,335 | 209（引用 `cvss_breakdown` 形状，不重列 8 指标） | −1,126 | −289 | 中性偏正：消除第三份 CVSS 说明 |
| 10 | `contextual_cvss_reasoning` | 645 | 115 | −530 | −136 | 无明显影响 |
| 11 | `reachability_evidence` | 913 | 141 | −772 | −198 | 中性 |
| 12 | dep `code_locations` | 661 | 186（引用 create 的同名字段） | −475 | −122 | 中性：形状确实相同 |
| 13 | `manifest_path` | 376 | 79 | −297 | −76 | 无明显影响 |
| 14 | `advisory_cvss` | 297 | 91 | −206 | −53 | 中性：优先级规则在 `contextual_cvss_breakdown` 里保留 |
| 15 | `introduced_by` | 237 | 93 | −144 | −37 | 无明显影响 |

### P2：类型层面的减法（实测 −1,396 字符 ≈ −358 tok）

把可选 `str | None = None` 改成 `str = ""`，schema 从 `anyOf[string,null]` 折叠成 `{"type":"string","default":""}`。实测在报告工具上省 **1,396 字符 ≈ 358 tok**（约 40 个可选字段，每个约 35 字符）。

- 影响：需要 `_do_create` / `_collect_update_changes` 把空串按"未提供"处理，否则 `""` 会覆盖已有值。**这是一次真正的行为改动**，收益却只有 358 tok，性价比最低，放最后或直接放弃。
- 列表型字段（`code_locations`、`http_exchange_ids`）不能这么改，`[]` 与 `None` 语义不同。

### 威胁建模侧（从 1,768 → 约 863 tok）

| 工具 | 现状 | 目标 | 改法 |
|---|---:|---:|---|
| `save_threat_model` | 2,849 | ~900 | 四段结构与 400 字符门槛是**运行时校验**（`tools.py:39-58`），描述只需列出 4 个标题名 + 指向 `analysis/threat_model` skill |
| `amend_threat_model` | 1,351 | ~450 | 80 字符下限与 40 条上限同样由常量校验，描述压缩到"追加一条修订，用于反证基线" |
| `get_threat_model` | 1,128 | ~450 | 删掉 `_not_found` 劝导语的重复叙述——那段话本来就在返回值里 |

合计 −3,528 字符 ≈ **−905 tok（−51%）**。你提到的"约 1.6k"应该是当前实测值（1,768）；压到 1.6k 几乎不用改，压到 0.9k 才需要上面这套。

---

## 五、总账

| 范围 | 现状 | 优化后 | 净减 | 比例 |
|---|---:|---:|---:|---:|
| 报告工具 | 9,435 tok | 5,489 tok | **−3,946** | −41.8% |
| 威胁建模 | 1,768 tok | 863 tok | **−905** | −51.2% |
| 子 Agent 工具面 | 23,511 tok | 18,660 tok | **−4,851** | −20.6% |

**折算到账单要打折。** schema 位于缓存前缀最前段，实测缓存命中 57%–74%，按 0.35 全价 + 0.65×0.1 加权，有效成本约为标称值的 0.42 倍：

- 单请求明文计数减少 4,851 tok；按 15 请求算，一个 run 少计约 **73k**（占 540k 的 13.5%）；
- 折算成计费当量约 **2,000 tok/请求**，一个 run 约 **30k**——相对 540k 是 5% 量级。

所以**不要用"省 token"来为这次改动辩护**。真正的收益排序是：

1. **修掉 `code_locations` 错位**——这是唯一影响产出质量的缺陷（1 处，白盒报告必受影响）；
2. **降低指令密度**——`save_threat_model` 用 2,849 字符描述 2 个参数，`create_vulnerability_report` 用 8,891 字符描述 24 个字段，长描述的边际收益递减，而模型在长描述里抓重点的失败率上升；
3. **多子 Agent 场景的冷启动**——新 Agent 首次请求必然缓存未命中，23.5k → 18.7k 是**全价**节省。但要注意当前实况：`agents.db` 15 份统计显示典型 run 只有 **2 个 session（root + 1 child）**，root 占 55%–65% 请求。按 0.6/0.4 加权，本方案的计数收益是 **0.4 × 4,851 ≈ 1,940 tok/请求**，而非对每个请求都成立。若后续转向"一节点一 Agent、用完即毁"，这一项才会变成主收益；
4. 计数意义上的 token 下降。

换句话说：**root 那边早就裁过（880 tok），真正的浪费在执行类子 Agent 身上**——它们在当前典型 run 里只占 40% 的请求，但每一个都按 11.2k 付费。

---

## 六、与 Sapper 对照后的取舍

**该学的三条**

1. **错误即文档**。Sapper 的 `validate_graph()` 在写后报错，报错内容就是规则。Ben 的 `_validate_fix_verification` 已经这么做了——那就应该把 schema 里的散文删掉，把 validator 当作唯一真相源。这是零成本删减，且不会丢失约束。
2. **单一词汇**。Sapper 所有子命令共用一套 `fact`/`intent` 字段。Ben 的 CVSS 说明散在 `create`、`create_dependency`、`update` 三处，`code_locations` 散在两处——收敛成"一处定义 + 其余引用"，是这次 41.8% 里最大的一块。
3. **按需加载**。Ben 已有 `load_skill` 和四份对应的 analysis skill，缺的不是机制，是**删掉 schema 里的副本**。

**不该学的两条**

1. **不要退化成 `key=value`**。Sapper 的 `-d severity=high` 自由字典换来 0 schema 成本，代价是没有字段级约束、没有 CVSS 一致性检查、没有 `duplicate_of` 去重。Ben 的 `_validate_analysis_fields`、`_reject_cross_class_revision`、`_fit_revision_to_class`（`tool.py:303-598`）正是靠具名参数才成立。**schema 可以瘦，字段名不能丢。**
2. **不要把规则搬到每轮注入的 hook 里**。Sapper 的 frontier 注入是状态驱动，Ben 没有等价的常驻状态需求；照搬会把一次性成本变成每轮成本。

---

## 七、落地顺序与验收

1. **P0-1 / P0-2（错位修复）**：只动 `tool.py:1126-1199` 的缩进与文本，`git diff` 应显示"同一段内容从一个字段移到另一个字段"。验收：`create_vulnerability_report` 的 `code_locations` 描述长度 > 400，`http_exchange_ids` < 400。
2. **P0-3 ~ P0-5**：删描述、留指针。验收：`tests/tools/test_reporting_tool*.py` 全绿；手工跑一次白盒上报，确认 `fix_after` 缺失时的报错仍完整。
3. **P1-6 ~ P1-12**：逐工具改，每个工具单独提交。验收：schema 总字符数从 36,795 降到 ≈21,400（可用本次的构建脚本复测）。
4. **威胁建模**：与 `docs/threat-model-lightweight-v1.md` 的 L1 合并执行，避免两次改同一批 docstring。
5. **P2 不做**，除非未来把"未提供"与"空串"的语义在 `_do_create` / `_collect_update_changes` 里彻底分离。

**同步要改的测试**：`tests/tools/test_reporting_tool*.py` 中断言描述文本的用例（若有）、`tests/agents/test_root_prompt_policy.py`（提示词策略），以及任何断言工具 schema 快照的测试。建议改前先 `grep -rn "Report output rules\|Nice to have\|For the orchestrator" tests/` 确认影响面。

---

## 附：复测方法

```bash
# 1) 生成仅含签名 + docstring 的桩模块（保存在 .workbuddy/tmp/，勿入库）
# 2) 构建 FunctionTool 并序列化
.venv/bin/python -c "
import sys, json, importlib
sys.path.insert(0,'.workbuddy/tmp')
m = importlib.import_module('schema_rep')
tot = sum(len(json.dumps({'name':o.name,'description':o.description,'parameters':o.params_json_schema}, ensure_ascii=False))
          for o in (getattr(m,n) for n in dir(m)) if type(o).__name__=='FunctionTool')
print(tot, 'chars ≈', round(tot/3.9), 'tok')
"
```

改动后该数字应从 36,795 降到 21,405（±5%）。若用了 `tiktoken`（需联网下载词表），以实际编码结果为准。
