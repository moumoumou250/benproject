# Skill 可验证改造方案（check / diagnose / verify）

状态：待评审（未实施）
日期：2026-11-19
基线：`Ben/` @ f9b304d

**前提**：这里的 KB 就是 `Ben/skills/vulnerabilities/*.md` 这 29 条技能，不涉及 `Ben/rag/` 语料库。
目标只有一件事：**每条 skill 都能被验证** —— 静态上能被机读校验，动态上能在靶场复现。

---

## 1. 为什么这个目标现在是可达的

盘点结果（已实测）：

| 事实 | 数值 |
|---|---|
| `vulnerabilities/*.md` 总数 | 29 |
| 含 `## Validation` 段 | **29 / 29** |
| 含 `## False Positives` 段 | **29 / 29** |
| `## Validation` 为编号/项目符号条目 | **29 / 29**，合计 **151 条**，每条 skill 4–7 条 |
| 散文式（无法逐条核对）的 Validation 段 | **0** |
| 按本方案 L0 规则预跑 | **29 / 29 通过**（已实测，不是估计） |
| frontmatter 键 | 只有 `name` / `description` |
| 别名 | `Ben/interface/cli/vuln.py:58` `_ALIASES` 46 条，硬编码在 CLI 里 |

也就是说，判据内容**已经写好了**，缺的不是内容，而是：

1. 没有机读方式判断"这一条 skill 是否具备被验证的条件"
2. 没有把 `## Validation` 的条目变成可逐条核对的 checklist
3. 没有在靶场上跑一遍并落证据的命令

所以本次改造是**加校验器和执行器**，不是补知识。

---

## 2. 可验证性标准（三级，逐级加严）

| 级别 | 含义 | 判据 | 谁来判 |
|---|---|---|---|
| L0 结构可验证 | 有判据载体 | frontmatter 合法、`name` 合规范、`description` 非空、`## Validation` 段存在且非空、`## False Positives` 段存在 | `ben kb check` |
| L1 判据可执行 | 判据能逐条核对 | 结构部分：`## Validation` 里是编号/项目符号条目（**已 29/29 满足**）；语义部分：每条是"可观察的陈述"而非"注意不要误报"这类空话 | `ben kb check --strict` |
| L2 靶场可复现 | 判据在真实目标上成立 | `ben verify` 逐条判定证据，落 `kb_evidence/` | `ben verify` |

默认 `check` 只跑 L0；`--strict` 加 L1。**L0/L1 结构部分在改造当天就会全绿**，这是预期的——
它的价值不是"发现问题"，而是**守住基线**：以后谁改了 skill 或渲染器，退化了立刻红。

L1 的语义部分（`is_checkable`）是启发式，只用于给 diagnose 提供 3 的归因，不作为准入闸门。

---

## 3. 三条命令

```
ben kb check [<term>] [--strict] [--json]                  → 0/1/2
ben kb diagnose <term> [--variant ...] [--json]            → 0/1/2/3
ben verify <term> --profile <p> [--max-budget] [--max-turns] → 0/1/2/3
```

### 3.1 `ben kb check`

不带 `<term>` 时校验全部 29 条，带则校验单条。

| 退出码 | 含义 |
|---|---|
| 0 | 全部通过当前级别 |
| 1 | 至少一条不通过（逐条打印缺什么） |
| 2 | 用法配置错：词表外 / skill 不存在 / YAML 非法导致根本无法解析 |

输出（人读）：

```
idor                     L0 ok   L1 ok    Validation 5 条 / False Positives 3 条
mass_assignment          L0 ok   L1 FAIL  条目 2 不是可观察陈述："注意不要误报"
broken-function-level-authorization  L0 FAIL  缺 ## Validation 段
```

### 3.2 `ben kb diagnose <term>`

只回答一件事：这条 skill 不能验证，是**叫法问题**还是**内容问题**。

| 退出码 | 归因 | 判据 |
|---|---|---|
| 0 | 叫法缺口 | `canonical(term)` 解析不出 → 该补 `ALIASES`；或 `--variant` 里有能解析的写法 |
| 1 | 内容缺口 | 能解析，但 `## Validation` 缺失 / 空 / 只有一句"人工判断" |
| 2 | 用法配置错 | 变体 < 2、skill 文件不可读、frontmatter 非法 |
| 3 | 无法归因 | Validation 段有内容但没有可逐条核对的条目（散文式）→ 既不是缺别名也不是缺内容 |

变体来源：规范名、`ALIASES` 反查别名、文件名下划线/连字符变体。少于 2 个直接 2。

### 3.3 `ben verify <term> --profile <p>`

流程：

1. 校验 profile：目标 ∈ `profile.targets` **且** ∈ `.env ALLOWED_TARGETS`（双闸），`term` ∈ `allowed_vulns` → 否则 2
2. 从 skill 抽 checklist（`## Validation` 的条目）+ 反例（`## False Positives`）
3. 无可解析条目 → **直接返回 3 `NO_VALIDATION`，不启动扫描**（省钱，也堵死"无判据却报确认"）
4. 调 `Ben.interface.cli.vuln.run_vuln(...)` 跑一轮聚焦扫描（复用：只加载对应技能、强制单子 agent、`--expect`/`--max-budget`/`--max-turns`）
5. 从 trace 抽请求/响应写 `kb_evidence/`
6. 逐条判定：满足哪几条、缺哪几条 → 映射退出码

| 退出码 | 判定 | 子原因 |
|---|---|---|
| 0 | CONFIRMED | —（必须打印满足了哪几条 Validation 原文 + 两行证据引用） |
| 1 | NOT_CONFIRMED | `NOT_VULNERABLE` / `BLOCKED`（**不得降级为前者**）/ `PAYLOAD_MISMATCH` |
| 2 | USAGE | profile / 词表 / 目标不合法 |
| 3 | INCONCLUSIVE | `NO_VALIDATION` / `UNREADABLE` / `BUDGET` / `CONNECTION_LOST` |

硬规则：没有可解析的 Validation 条目禁止返回 0；返回 0 必须打印所依据的判据原文。

---

## 4. 改造清单

### 新增

```
Ben/kb/__init__.py            对外入口
Ben/kb/vocab.py               词表（从 vuln.py 迁入）+ canonical() + 孤词/悬挂校验
Ben/kb/schema.py              单条 skill 的 L0/L1 校验，返回缺项列表
Ben/kb/criteria.py            从 ## Validation / ## False Positives 抽条目
Ben/kb/probe.py               check/diagnose 归因逻辑（纯函数，文件内容以参数注入）

Ben/interface/cli/kb.py       run_kb(argv)
Ben/interface/cli/verify.py   run_verify(argv)

tests/profiles/*.yaml
tests/kb/test_schema.py
tests/kb/test_criteria.py
tests/kb/test_vocab.py
tests/interface/test_kb_cli.py
tests/interface/test_verify_cli.py

docs/kb-iterations.md
kb_evidence/                  运行时产出，gitignore
```

### 修改

| 文件 | 改动 |
|---|---|
| `Ben/interface/main.py:478` | 分发链加 `kb`、`verify` 分支 |
| `Ben/interface/cli/vuln.py` | `_ALIASES` 删掉，改 `from Ben.kb.vocab import ALIASES`；`--list` 展示改读 vocab |
| `Ben/interface/cli/completions.py` | `_ROOT_COMMANDS` 加 `kb`/`verify`；新增对应 flag 表 |
| `Ben/skills/vulnerabilities/*.md` | **不改**（29/29 已有 Validation） |

### 不动

- `Ben/rag/` 全部（含 `vector_store.search`、`only`、embedding）——本方案与其无关
- `Ben/core/`、`Ben/agents/`、`Ben/tools/`
- 技能的渲染路径：`_parse_skill_file` 只读 `name/description/template`，加任何校验代码都不影响 prompt

---

## 5. 判据抽取（`Ben/kb/criteria.py`）

```python
@dataclass(frozen=True)
class Criterion:
    index: int          # 段内序号
    text: str           # 原文，一字不改（返回 0 时要打印）
    kind: str           # "numbered" | "bullet" | "prose"

def extract_validation(body: str) -> list[Criterion]
def extract_false_positives(body: str) -> list[Criterion]
def is_checkable(c: Criterion) -> bool   # L1：有动词 + 可观察对象，且不是"注意/小心/避免"这类
```

`kind == "prose"` 或 `is_checkable() == False` 的条目，在 verify 里标为 `UNREADABLE`，在 diagnose 里触发 3。

以 `idor.md` 为例，抽出来是 5 条，其中第 5 条 `Provide reproducible steps and evidence` 属于
"过程要求"而非"可观察陈述"，`is_checkable()` 返回 False —— 这类条目只作为输出格式要求，不参与确认判定。

---

## 6. 结论 → 改哪里

| 触发 | 改什么 | 成本 |
|---|---|---|
| diagnose 0 叫法缺口 | `Ben/kb/vocab.py` ALIASES | 小 |
| diagnose 1 Validation 缺失 | 补 `## Validation` 段 | 中 |
| diagnose 3 散文式判据 | 把该段改成编号条目 | 小 |
| verify 3 UNREADABLE | 判据读的东西（状态码/正文/头）与证据对不上，改条目措辞 | 小 |
| verify 1 PAYLOAD_MISMATCH | skill 里的路径/参数假设 | 小 |
| verify 1 BLOCKED 且无绕过节 | skill 补绕过节 | 中 |
| verify 1 NOT_VULNERABLE 且 check 1 | 补该 skill 的测试方法 | 大 |

依赖顺序：**A 补判据 → A 改解析 → B 修假设 → B 补绕过 → C 补别名 → B 补内容 → D 扩词表**。
C 必须在 B 之前（别名五分钟，写内容两小时）。

---

## 7. 实施步骤

### S1 词表迁出（纯搬运，最小风险）

- `_ALIASES` 从 `vuln.py:58` 搬到 `Ben/kb/vocab.py`，`vuln.py` 改 import
- `canonical(term)`：ALIASES 精确命中 → 规范化（`_`/`-` 互换、小写）→ 匹配 skill 名 → None
- 验收：`ben vuln --list` 输出与改动前**逐字节一致**；`ben vuln sqli --dry-run` 仍能解析

### S2 `ben kb check`（L0）

- `schema.py`：校验 frontmatter 合法、`name` 匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`、`description` 非空、
  `## Validation` 存在且非空、`## False Positives` 存在
- `cli/kb.py` + `main.py` 分支
- 验收：对 29 条实跑，**预期 29/29 L0 全绿**（已预跑确认；任何一条红都说明校验规则写错了，不是 skill 有问题）

### S3 `--strict`（L1）+ `diagnose`

- `criteria.py` 抽条目、`is_checkable()`
- `probe.py` 归因四分类
- 验收：L1 结构部分 29/29 通过（151 条全部为编号条目）；`is_checkable()` 统计每条 skill 的可判定比例，
  人工抽查 5 条确认误判率；人为构造 4 个坏样本（缺段/空段/散文式/缺别名）验证退出码 1/1/3/0

### S4 `ben verify` 单类打通（file-upload）

- `tests/profiles/local-file-upload.yaml`、`verify.py`
- 三条路径必须跑到：`NO_VALIDATION`、`CONFIRMED`、`UNREADABLE`
- 验收：靶场实跑一次，CONFIRMED 时能指着两行请求/响应说清满足了哪几条

### S5 真实失败回补 + 记录

- 按 §6 对一次真实失败做**一个**动作改动
- 写 `docs/kb-iterations.md` 一行，复跑 `ben kb check <term>` 全绿

**记录表头**：

```
| 日期 | target | vuln | verify前 | diagnose | 动作类 | 改动文件 | verify后 | check后 |
```

规则：一次记录只允许一个动作；写了"之后"才允许提交；未复跑视为未完成。

---

## 8. 验收

- `ben kb check` 对 29 条 L0 全绿
- `ben kb diagnose` 对 4 个人造坏样本给出 0/1/2/3 正确归因
- `ben verify` 的 CONFIRMED 能指着请求/响应说清满足哪几条 Validation
- `ben vuln --list` 与改造前输出一致
- 词表无孤词（alias 指向存在的 skill）
- 迭代记录连续 3 条完成"前/后"配对

---

## 9. 风险

1. **`is_checkable()` 是启发式**：必然有误判（`idor.md` 第 5 条"Provide reproducible steps and evidence"
   就不该参与确认判定）。错了就当一个 bug 修，不要为了让它通过而改判据文案——那是把校验器调成永远通过。
   因为它只影响 diagnose 的 3 号归因，误判不会污染 check 的绿/红。
2. **verify 的逐条判定依赖模型**：判据是英文散文，证据是 HTTP 报文，中间需要一次判断。
   本方案不做结构化 `validation_checks:` 字段（29×3 条太大且未经验证），先用模型判定 +
   打印原文。若发现模型判定不稳，再对高频类型（idor/upload/xss）局部结构化。
3. **`## Validation` 里混着"过程要求"**（如 idor 第 5 条是输出格式要求）。这些不参与确认判定，
   用 `is_checkable()` 排除，但**不删原文**——它们对 agent 仍是有效指令。
4. **S4 依赖靶场**：无靶场时先用假 runner 验退出码映射，实跑推到环境就绪。

---

## 10. 不做

自动改 skill、自动利用链、横向、报告渲染、RAG 向量检索改造、给 skill 加机器可读的
`validation_checks` 结构（等真实失败暴露需求再说）。
