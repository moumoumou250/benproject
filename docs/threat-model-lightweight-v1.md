# Ben 威胁建模：作用定位与轻量化方案 v1

> 日期：2026-11-17
> 依据：`Ben/tools/threat_model/tools.py`（638 行）、`Ben/skills/coordination/root_agent.md`、`Ben/agents/prompts/system_prompt.jinja`、20 个真实 run 的统计（`strix_runs/*/`）
> 目标：在不放弃"定级锚点"的前提下，把威胁建模的常驻开销、读取开销、流程开销各降一档

---

## 0. 一句话结论

**这个模块的作用只有一个：给"严重度"提供本目标的判据。** 其余三件事（一致性、可达性、攻击面排除）都是它的副产品。

**它的重量几乎全在文本上，不在代码上**：638 行代码里真正承载语义的不到 300 行，而每个请求恒定背着 6,535 字符的工具说明、root 每轮再背 2,246 字符的方法论、每个读者再吃 3,200 字符的模型正文。因此轻量化的正确做法是**先瘦文本、再条件化触发、最后才动机制**，而不是重构模块。

---

## 1. 作用：四个消费点，一个根

| 作用 | 谁消费 | 代码位置 | 强度 |
|---|---|---|---|
| **定级锚点**：本目标里 critical/high/medium/low 各长什么样 | 写报告的子 agent、root 分诊 | `tools.py:570-573` 要求四段之一必须有具体例子 | **强**（唯一不可替代） |
| **可达性前提**：认证前/低权限/跨租户分别能不能打到 | 判定同一条漏洞的严重度 | `tools.py:559-564` | **强** |
| **攻击面排除**：哪些是 dev-only / 测试 / 脚本，不算线上 | 抑制误报 | `tools.py:543-552` | 中 |
| **多 agent 一致性**：5 个 agent 用同一套"攻击者可控"定义 | root 与所有子 agent | `root_agent.md:29-37` | 中（单 agent 时不成立） |

消费关系很薄，值得说清：

- **报告侧是唯一硬消费**：`root_agent.md:37` 要求写最终报告前回读 amendments，"一个 agent 告诉你某边界其实可达，这是关于模型的发现"。
- **SARIF 侧是弱消费**：`Ben/report/sarif.py:104-130` 的 `stride:<leg>` 标签复用 STRIDE 分腿概念，但**不读模型正文**。
- **前端侧是展示消费**：`ThreatModelRenderer.tsx` 只渲染，不推理。
- **没有第三个硬消费点。** 这意味着：如果去掉"定级锚点"，整个模块就没有存在理由——反过来说，轻量化不能动 `Severity Calibration`。

---

## 2. 重量在哪：三层文本 + 一轮预算

### 2.1 常驻文本（每个请求都付）

| 项 | 体量 | 计费特征 | 位置 |
|---|---|---|---|
| `save_threat_model` docstring | 3,311 字符 ≈ 1,034 tok | 每请求、每个 agent | `tools.py:521-581` |
| `amend_threat_model` docstring | 1,761 字符 ≈ 550 tok | 同上 | `tools.py:592-638` |
| `get_threat_model` docstring | 1,463 字符 ≈ 457 tok | 同上 | `tools.py:481-517` |
| **小计** | **6,535 字符 ≈ 2,042 tok** | 工具 schema 位于请求最前段 | `factory.py:1065-1067` 无条件注册 |
| root 方法论段 | 2,246 字符 ≈ 702 tok | root 每轮 | `root_agent.md:29-37` 恒定内联 |

注意一个**账单与计数的差异**（本项目已实测确认，见 `docs/ledger-optimization/README.md` 与台账复核记录）：工具 schema 在缓存层级最前（tools → system → messages），大概率整段命中缓存。所以这里的 2,042 tok 是**计数收益**，折算成账单约为 0.1x，即 ~200 tok 等效/请求。

**真正的账单大头不是 schema，是轮次**：每多跑一轮就多一整份 20K+ 上下文。

### 2.2 读取文本（每个读者每次付）

实测 3 个真实模型：2,157 / 3,457 / 4,055 字符，平均 **3,223 字符 ≈ 1,007 tok**。全文经 `get` 进入读者会话，并在其后每一轮重复计费。

`_get_impl`（`tools.py:296-324`）没有任何分层，`content` 原样返回。

### 2.3 流程开销

- `save` 门槛：≥400 字符 + 四段标题齐全（`tools.py:39-58`、`338-361`）；
- `amend` 门槛：≥80 字符、最多 40 条（`tools.py:40-42`）；
- 二次 `save` 收编：覆盖全文并**清空** amendments（`tools.py:364-389`），需要 root 专门花一轮；
- `972f` 实测：8 轮预算的 root 在第 2、3 轮做取+存，占 **25% 轮次**。

### 2.4 实际被消费的程度（20 run 统计）

| 指标 | 实测 |
|---|---|
| 建过模型的 run | 3 / 20 |
| 平均模型正文 | 3,223 字符 |
| amendments 总数 | **0** |

结论：**写的人少、写得长、没人改。** 三个数共同指向一个判断——门槛主要是形式负担，精细度没有被消费。

---

## 3. 轻量化四档

按"收益/风险"排序，L0 与 L2 可以先做，L1 次之，L3 看使用情况。

### L0｜触发条件化（零代码，收益最大）

**现状**：`root_agent.md:31` 开头就是 "Every scan needs one shared answer"，`system_prompt.jinja:168/184-185` 无条件列出工具族，`_resolve_skills` 对 root 恒定内联该片段（`prompt.py:88-97`）。模型因此默认就会建。

**改法**：把"必须"改为"满足任一才建"，四条判据：

1. 多 agent 并行（≥2 个子 agent 会产出 finding）；
2. 扫描目标 ≥2 个，或存在多个租户/角色；
3. 定级依赖可达性（存在认证后接口，或后台/管理面）；
4. 白盒仓库，且"哪些目录是线上"本身是未知。

四条都不满足 → 明确写"跳过，直接测"。同时给出跳过的正确姿势：**不要先 `get` 再决定**——未命中的返回语（`tools.py:281-293`）会强烈劝你去派生。

**收益**：在窄任务上省 2 轮（8 轮预算下 25%）；对全量扫描基本无变化。
**风险**：低。纯提示词，可回滚。
**代价**：需要改 `tests/agents/test_root_prompt_policy.py:15` 的断言（若改标题）。

### L2｜读取分层（小改，收益稳定）

**改法**：`get_threat_model` 增加 `detail: Literal["summary", "full"]`，默认 `summary`：

- `summary`：只返回 **Trust Boundaries** + **Severity Calibration** 两段的压缩文本（目标 ≤600 字符），外加 `sections_available: [...]` 与提示"需要攻击面细节时用 `detail="full"`"；
- `full`：现有行为，`content` 全文 + amendments。

实现要点：

```python
_SUMMARY_SECTIONS = ("trust boundaries", "severity calibration")

def _summarize(content: str, limit: int = 600) -> str:
    """按二级标题切段，只保留边界与定级两段，超长按段截断。"""
    # 逐段匹配 "## " 开头的标题；标题命中 _SUMMARY_SECTIONS 即收录
    # 未命中任何标题时退化为 content[:limit]
```

**收益**：单次读取 3,223 → ~600 字符，注入量 −80%；按 3 个读者 × 5 轮估算，一次 run 少注入约 12-15k tokens；更重要的是**执行者上下文不再被 Attack Surface 长文稀释**。
**风险**：中。默认值改变属于行为变更，需同步 `tests/tools/test_threat_model_tool.py`（`test_saved_model_round_trips`、`test_amendment_is_returned_with_the_model` 等会断言 `content`）。
**折中**：若不想改默认值，可先加参数但默认 `full`，在 `root_agent.md` 里要求子 agent 用 `detail="summary"`——代价是依赖模型守规矩。

### L1｜schema 瘦身（中等改动，计费收益打折）

**改法**：三个 docstring 压到"何时用 / 参数 / 返回"，写作纪律迁出：

| 迁出内容 | 去处 |
|---|---|
| 四段结构定义、写作要求（`tools.py:554-573`） | 新建 `Ben/skills/analysis/threat_model.md`，由 root 内联、子 agent `load_skill` 拉取 |
| "覆盖还是追加"的取舍说明（`tools.py:527-533`） | 同上 |
| 目标身份归一化说明（`tools.py:493-497`） | 留在 docstring 一句即可，细节在模块顶部 docstring |

目标：6,535 → ~2,400 字符（−63%），计数 −1,300 tok/请求。
**风险**：中。`load_skill` 有预算（≤5 个 / ≤24K 字符），需确认 root 内联清单有余量。
**诚实预期**：计数收益大，**账单收益小**（缓存命中，约 0.1x）。真正的价值是让模型不必在 1,034 字符的 `save` 说明里找重点。

### L3｜机制减法（按需，最后做）

三处可选裁剪，互不依赖：

1. **门槛分档**：新增 `brief` 档——正文 ≥200 字符 + 三段（Overview / Boundaries / Severity），`Attack Surface` 转为可选。现状 400 字符四段对"只想记一句边界"的场景太重。实现：`_REQUIRED_SECTIONS` 拆成 `_REQUIRED_FULL` 与 `_REQUIRED_BRIEF`，`_save_impl` 按档校验。
2. **`save` 归档而非清空**：旧版压入 `revisions[]`（保留 3 版），把"别忘了抄回去"从提示词保障变成结构保障（`tools.py:379-383` 现在是纯提醒）。
3. **砍掉二次收编仪式**：既然实测 0 修订，可去掉"root 必须二次 `save` 折叠"的流程要求（`root_agent.md:37` 后半句），让 revisions/amendments 自然累积，报告生成时再读全部。

**不建议做的第三项替代**：把 `amend` 整个砍掉、工具数 3→2。省 ~550 tok schema，但会丢失"子 agent 反证边界"的唯一通道——而这正是当前最该加强的能力（见 §7）。

---

## 4. 推荐组合

| 阶段 | 内容 | 省什么 | 改动面 |
|---|---|---|---|
| 第一步 | **L0** | 2 轮（窄任务）、提示词 0 |
| 第二步 | **L2**（默认 summary） | 读取 −80%，一次 run −12~15k tok 注入 | 1 个函数 + 4-6 个测试 |
| 第三步 | **L3-1 + L3-2** | 门槛降一半；修订不再丢 | `_save_impl` + 常量 |
| 可选 | **L1** | 计数 −1,300 tok/请求（账单 ~−130） | 3 个 docstring + 新 skill 片段 |

L1 排后面是因为它收益在计数上、风险在案上（改 schema 要跑全量工具面测试），而 L0/L2 的收益直接体现在轮次和上下文质量上。

---

## 5. 落地时要同步的东西

| 位置 | 为什么 |
|---|---|
| `tests/tools/test_threat_model_tool.py` | 29 个用例断言 `content` 全文、四段校验、amendments 结构 |
| `tests/agents/test_root_prompt_policy.py:15,29` | 断言 `## Establish the Threat Model` 在 root 有、child 无 |
| `root_agent.md:29-37` | L0 的条件化、L3 的收编仪式都落在这里 |
| `system_prompt.jinja:168/184-185` | 工具族说明若改默认 `detail`，此处要提一句"默认只返回摘要" |
| `Ben/interface/viewer/.../ThreatModelRenderer.tsx` | 若 `get` 返回结构变化（新增 `sections_available`），前端卡片可展示但不必改 |

**回归判定**：单跑 `tests/tools/test_threat_model_tool.py` + `tests/agents/test_root_prompt_policy.py`；本机全量 pytest 有 ~99 个预先存在的环境失败，以该基线比较。

---

## 6. 轻量化≠删除：三条硬边界

1. **保留 `Severity Calibration`。** 这是模块唯一不可替代的输出。20 个 run 里 17 个没建模也能跑完，说明"不做"成本很低；但一旦要做报告，定级没有本目标锚点就只能套 CVSS 通用值——这正是 Sapper 的短板，别主动退化过去。
2. **保留 `Trust Boundaries`。** 可达性是唯一能改变"同一条漏洞 critical 还是 medium"的信息。
3. **保留 `amend`。** 它是子 agent 反证基线的唯一通道。当前 0 使用是流程问题（没人要求它用），不是机制问题——解决办法是补一个 blindspot 审查角色，不是删掉通道。

---

## 7. 一个附带建议（与轻量化同向）

上一轮分析建议给 `recon` / `enum` profile 补 `get_threat_model`（`profiles.py:81-145` 缺失）。**如果目标是轻量化，这条应撤回**：侦察类 agent 的执行不需要定级锚点，让它读 600 字符的 summary 也是纯开销。保持这两个 profile 精简，只在 `exploit` / `validate` / `default`（`tool_names=None`）保留工具面，与轻量化方向一致。

真正该补的是反面：**给威胁模型加一个"对手"**（blindspot 式审查，3-7 条带 risk 的质疑）。它增加的是分析质量，不增加常驻文本——比扩 profile 划算。
