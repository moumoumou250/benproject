# Skill 三命令方案 v2：check / diagnose / verify

状态：S1–S4 + transcript P1–P4 已落地（2026-11-19）  
S4b：`BEN_AUDIT` / AuditWriter / `ben transcript` / spill 双写 / `verdict.transcript` 已接线。  

日期：2026-11-19  
基线：以工作区实际代码为准（原 `f9b304d` 基线不成立）  

取代：`docs/kb-feedback-loop-plan-v1.md`（v1 用 `ben kb check` / `ben verify` 两命令命名，本文件改为单命名空间 `ben skill <verb>`，并补齐「结果 → KB 动作」的代码化与三层不越界的可执行约束；v1 保留作历史记录，不再实施）

**前提**：本方案的 KB 就是 `Ben/skills/vulnerabilities/*.md` 这 29 条技能，不涉及 `Ben/rag/` 语料库。  
目标只有一件事：**每条 skill 都能被验证** —— 静态上能被机读校验，动态上能在靶场复现并留下可复核的证据。

**正样本前提（找 skill 不足时强制）**：用于 `verify` 的 profile 默认 `expects_vuln: true` —— 靶场**确定暴露**对应用户要验的漏洞类。因此 `verify` 退出码 `1` **不再**表示「目标没洞」，而表示「skill 指导下未能复现已知洞」（假阴性）。不动 agent 主链大改；只改 KB 契约、子原因与动作表。

---

## 0. 摘要

新增三个命令，共用一个命名空间：

```
ben skill check    [<term>|--all] [--strict] [--json]
ben skill diagnose <term> [--variant ...] [--json]
ben skill verify   <term> --profile <p> [--mode scan|probe] [--max-budget N] [--max-turns N] [--json]
```

三层职责严格不重叠：

| 命令         | 回答什么           | 发网络请求    | 写盘     | 退出码                                                      |
| ---------- | -------------- | -------- | ------ | -------------------------------------------------------- |
| `check`    | 知识取得到吗         | 否        | 否      | 0 可达 / 1 不可达 / 2 用法配置错                                   |
| `diagnose` | 取不到是叫法问题还是内容问题 | 否        | 否      | 0 叫法缺口 / 1 内容缺口 / 2 用法配置错 / 3 无法归因                       |
| `verify`   | 正样本上 skill 能否复现 | 是（受预算约束） | 只写证据目录 | 0 CONFIRMED / 1 FALSE_NEGATIVE* / 2 USAGE / 3 INCONCLUSIVE |

\* 正样本 profile（`expects_vuln: true`）下码 `1` 的语义是假阴性；负样本 profile（显式 `expects_vuln: false`）才保留「目标无洞」语义，本方案找不足时不走负样本。

三条命令的价值不在"发现问题"，而在于**把 Skill 的质量变成可回归的观测值**：改一条 skill、动一次渲染器、换一个靶场，跑一遍就知道退化在哪一层。找不足时优先读 `verify 1` 的子原因（`MISS_SURFACE` / `TECHNIQUE_MISS` / `BLOCKED` / `FALSE_NEGATIVE`），再按 §7 落动作。

---

## 1. 现状实测（所有设计取舍的依据）

盘点结果全部实测，不是估计：

| 事实             | 数值                                                                             | 对设计的影响                                         |
| -------------- | ------------------------------------------------------------------------------ | ---------------------------------------------- |
| KB 条目          | `Ben/skills/vulnerabilities/*.md` 共 **29** 条                                   | 全量校验的成本可忽略，`--all` 是默认用法之一                     |
| 判据载体           | `## Validation` **29/29**、`## False Positives` **29/29**                       | 内容已经写好了，缺的是校验器与执行器，不是补知识                       |
| 判据形态           | 全部为编号/项目符号条目，合计 **151** 条（每条 4–7 条），散文式 **0**                                  | 机读抽取可行，无需先做结构化改造                               |
| frontmatter 键  | 只有 `name` / `description`                                                      | 没有 tags/title，"补别名"不能塞 description（见下条）        |
| description 截断 | `Ben/skills/__init__.py:_CATALOG_DESCRIPTION_MAX = 40`                         | 往 description 塞别名会被截掉；别名必须另有载体                 |
| 文件名 vs 规范名     | **22/29 不一致**（stem 全 `snake_case`，`name` 全 `kebab-case`）                       | 用户从 `ls` 抄名字必然解析失败，是最大的"叫法缺口"来源                |
| 名称解析           | `Ben/skills/__init__.py:resolve_skill_name` 只按 frontmatter `name` 匹配，不认 stem   | `sql_injection` 这类写法解析不了，且会在 stderr 留 warning  |
| 坏文件处理          | `_parse_skill_file` 是 **warn-and-skip**，返回 `None`                              | 坏条目从 catalog 里直接消失 → check 不能只看 catalog，必须三源求差 |
| 绕过段            | `## Bypass Techniques` **14/29**，另有 **2** 条写 `## Bypass Methods`               | 「条目无绕过节」的判定必须多标题白名单，否则 15 条误触发 B 动作            |
| 词表             | `Ben/interface/cli/vuln.py:_ALIASES` 46 条，硬编码在 CLI 里                           | 词表要迁出到 `Ben/kb/vocab.py`，供三命令共用                |
| 证据落盘           | `trace_*.json` 只存 root 的 `tool_invocations`，且 `output[:2000]` 截断；子 agent 报文不入盘 | **trace 不能当 verify 的证据源**（v1 稿的假设不成立，本方案已改）    |
| 结束原因           | `stopped_reason` 是中文自由文本（`Ben/core/runner.py:1282-1336`）                       | 无法映射 BUDGET / CONNECTION_LOST，需新增结构化字段         |
| 退出码现状          | 0 正常 / 1 环境或预检失败 / 2 `--fail-on` / 3 `--expect` 未命中 / 130 中断                   | 新命令码位独立，但 3 与 `--expect` 语义不同，需在 help 里说明      |

---

## 2. 命令面

### 2.1 为什么必须是两段式 `ben skill check`

`Ben/interface/main.py:496` 已把 `check` 当作 preflight 的别名：

```python
if len(sys.argv) > 1 and sys.argv[1] in ("preflight", "check", "target-check"):
```

因此顶层 `check` 不可用，也不应改动（会破既有用户脚本）。方案在所有新增命令前加一层 `skill` 命名空间，语义上正好对应「对象是 KB」，与 `ben vuln`（对象是一次扫描）区分开。

### 2.2 分发

`main.py:main()` 手写 `sys.argv[1]` 前缀分发，新分支与 `vuln` 同构：

```python
if len(sys.argv) > 1 and sys.argv[1] == "skill":
    from Ben.interface.cli.skill import run_skill
    return run_skill(sys.argv[2:])
```

放在 `vuln` 分支之后、`parse_arguments()` 之前。漏加会让 `ben skill check idor` 被主 parser 当成目标串解析，报一个莫名其妙的授权错误。

`Ben/interface/cli/completions.py` 的 `_ROOT_COMMANDS` 加 `"skill"`，并新增 `_skill_candidates()`；`zsh/bash/fish` 三套生成器共用同一份 flag 表。

### 2.3 `ben skill check`

不带 `<term>` 或 `--all` 时校验全部 29 条（基线守门）。

| 参数         | 说明                                                           |
| ---------- | ------------------------------------------------------------ |
| `<term>`   | 可选。用户写的叫法，走 `canonical()` 归一                                 |
| `--all`    | 校验全部条目（与 `<term>` 互斥，同时给报 2）                                 |
| `--strict` | 追加"判据可核对"要求：`## Validation` ≥1 条编号条目、`## False Positives` 存在 |
| `--json`   | 机器可读输出                                                       |

默认**只判可达**，`--strict` 才判结构。理由：三层职责要求 check 只回答"取不取得到"；把内容质量塞进 check，会让 `1 不可达` 同时意味着"缺别名"和"缺判据"，diagnose 就失去归因空间，§7 的 C 类动作也无从触发。`--all --strict` 保留为 CI 基线（当前 29/29 预期全绿，任何一条红都说明校验规则写错了，不是 skill 有问题）。

人读输出：

```
idor                              可达    Validation 5 条 / FP 3 条
sql-injection                     可达    Validation 6 条 / FP 4 条
sql_injection                     不可达  文件存在，frontmatter name=sql-injection（叫法缺口 → diagnose）
broken-function-level-authorization 不可达  loader 已丢弃（frontmatter 非法）
```

`check` 只给**一行原因**，不做归因（归因是 diagnose 的职责）。这一行的存在是为了让人不假思索地知道下一步该跑哪条命令。

### 2.4 `ben skill diagnose`

| 参数          | 说明                           |
| ----------- | ---------------------------- |
| `<term>`    | 必填，缺失报 2                     |
| `--variant` | 手动补充候选写法，可重复；用于验证"这个叫法能不能解析" |
| `--json`    | 机器可读输出                       |

变体来源（自动）：规范名、`ALIASES` 反查别名、frontmatter `aliases`、**文件名 stem**、stem 的 `_` ↔ `-` 互换、大小写折叠。22/29 的命名错位让 stem 变体成为这里最有价值的一条。

> **D3 已锁定**：**仅当 term 解析失败且变体 < 2 时返回 2**；term 能解析时正常归因，只在 JSON 里标 `"variants": 1` 作为弱证据，并在人读输出提示"该条目没有别名，反查能力有限"。`--strict-contract` 可恢复"变体 < 2 一律 2"的原语义。本项目有 7 条（csrf / idor / rce / ssrf / ssti / xss / xxe）无别名且 stem == name，变体天然只有 1 个。

人读输出（三行内说清归因 + 下一步）：

```
sql_injection  → 0 叫法缺口
  变体可解析：sql-injection（frontmatter name）、sql-injection（stem 互换）
  文件：Ben/skills/vulnerabilities/sql_injection.md
  动作：C 补别名（frontmatter aliases）—— 5 分钟
```

### 2.5 `ben skill verify`

| 参数                   | 说明                                         |
| -------------------- | ------------------------------------------ |
| `<term>`             | 必填                                         |
| `--profile <p>`      | 必填，靶场描述，见 §4.1                             |
| `--mode scan\|probe` | 默认 `scan`；`probe` 为零模型零写盘模式（S6 增量，见 §9）    |
| `--max-budget USD`   | 默认取 profile，缺省 0.30                        |
| `--max-turns N`      | 默认取 profile，缺省 10                          |
| `--minutes N`        | 墙钟上限，走 `Ben/core/scan_clock` 的 deadline 机制 |
| `--out-dir`          | 覆盖证据目录（默认见 §4.2）                           |
| `--json`             | 机器可读输出                                     |

执行顺序固定，**前五步全部不启扫描**（第 5 步只发 oracle 预检，不调 `run_vuln`）：

1. `canonical(term)` 解析 → 失败即 2（USAGE，词表外）
2. 从 skill 抽判据（`## Validation` + `## False Positives` 反例）
3. 判据为 0 条 → **立即 3 `NO_VALIDATION`，不启动扫描**（省钱，并堵死"无判据却报确认"）
4. profile 闸门：`profile.targets` 每一项都必须命中 `.env ALLOWED_TARGETS`（`Ben/config/settings.py:516 require_allowed_targets`），且 `term` ∈ `profile.allowed_vulns`；正样本还要求 `expects_vuln: true` 且 `oracle` 字段齐全 → 否则 2
5. **oracle 预检**：按 profile.oracle 发最小已知可复现请求并落盘到 `oracle/`；失败 → **3 `ORACLE_FAILED`**（靶场/洞漂移，不修 skill）
6. 调 `run_vuln()` 跑一轮聚焦扫描（复用：只挂该类技能、强制单子 agent、`--max-budget` / `--max-turns`）
7. 从证据源抽原始报文（§5）；对照 oracle.path 计算 `surface_hit`
8. 逐条判定（`expected_criteria` 优先）→ 映射退出码 → 写 `verdict.json` → 打印

---

## 3. 退出码与子原因契约

### 3.1 常量即唯一真相

`Ben/kb/contract.py`：

```python
class ExitCode(IntEnum):
    OK = 0
    NEGATIVE = 1
    USAGE = 2
    INCONCLUSIVE = 3

class SubReason(StrEnum):
    # verify 1（正样本：均为 skill 假阴性拆分，不得互相降级）
    BLOCKED = "BLOCKED"                 # 有拦截特征；不得降级为 FALSE_NEGATIVE
    MISS_SURFACE = "MISS_SURFACE"       # oracle 路径可达，但 agent 从未打到该面
    TECHNIQUE_MISS = "TECHNIQUE_MISS"   # 打到了面，但 payload/手法不对（含旧 PAYLOAD_MISMATCH）
    FALSE_NEGATIVE = "FALSE_NEGATIVE"   # 正样本上其它失败（打到面、无拦截、判据 FAIL）
    # verify 1（仅 expects_vuln: false 的负样本 profile）
    NOT_VULNERABLE = "NOT_VULNERABLE"   # 找不足流程禁用；保留常量以免旧测试字面量漂移
    # verify 3
    NO_VALIDATION = "NO_VALIDATION"     # KB 侧
    UNREADABLE = "UNREADABLE"           # KB 侧
    BUDGET = "BUDGET"                   # 环境侧
    CONNECTION_LOST = "CONNECTION_LOST" # 环境侧
    ORACLE_FAILED = "ORACLE_FAILED"     # 环境侧：正样本 oracle 预检未通过（洞已消失/靶场挂了）
    # diagnose 侧不使用 SubReason，用 Attribution
```

`PAYLOAD_MISMATCH` 并入 `TECHNIQUE_MISS`（路径/参数假设错误也是手法缺口）。代码里可保留 `PAYLOAD_MISMATCH` 作为 `TECHNIQUE_MISS` 的别名常量一版，测试只断言 `TECHNIQUE_MISS`。

CLI、JSON、测试全部引用常量，不允许出现字面量 `0`/`1`/`2`/`3`。这条不是洁癖：`--expect` 用 3、`--fail-on` 用 2，两套码位已在同一 CLI 里共存，再出现裸字面量必然有人混淆。

### 3.2 三张表

**check**

| 码 | 含义    | 触发                                               |
| - | ----- | ------------------------------------------------ |
| 0 | 可达    | term 解析成功 且 ∈ loader 载入表（`--strict` 时还要求判据段合规）   |
| 1 | 不可达   | 解析失败 / 文件不存在 / 文件被 loader 丢弃 / 段缺失（仅 `--strict`） |
| 2 | 用法配置错 | 参数冲突、`<term>` 与 `--all` 同时给、技能目录不可枚举、YAML 整体崩溃   |

**diagnose**

| 码 | 归因    | 触发                                                        |
| - | ----- | --------------------------------------------------------- |
| 0 | 叫法缺口  | term 解析不出，但某个变体能解析（含 stem 互换）；或全部变体都解析不出（词表外）             |
| 1 | 内容缺口  | 能解析，但不在载入表（文件被丢弃 / 条目不存在），或 `## Validation` 缺失/为空         |
| 2 | 用法配置错 | term 缺失、变体 < 2 **且** term 解析失败（见 D3）、文件不可读、frontmatter 非法 |
| 3 | 无法归因  | 判据存在，但没有任何一条可逐条核对（散文式，或全部 `is_checkable() == False`）      |

**verify**（`expects_vuln: true` 正样本；找 skill 不足的默认路径）

| 码 | 判定            | 子原因              | 触发条件（有序判定，先命中先返回） |
| - | ------------- | ---------------- | ------------------- |
| 0 | CONFIRMED     | —                | `expected_criteria`（若声明）全部机械 PASS，且每条引用≥1 条 raw exchange；未声明时要求全部 `checkable` 判据 PASS |
| 1 | FALSE_NEG     | BLOCKED          | 403/406/429/503 + 拦截特征（WAF 页、challenge、网关页） |
| 1 | FALSE_NEG     | MISS_SURFACE     | oracle 预检通过，但证据/请求日志中**从未出现** oracle 的 path（或 method+path）；agent 没摸到洞面 |
| 1 | FALSE_NEG     | TECHNIQUE_MISS   | 打到 oracle 面（或同类基线路径可达），但 404/400/405/422 或 payload 与 oracle 期望不符 |
| 1 | FALSE_NEG     | FALSE_NEGATIVE   | 打到面、无拦截，可核对判据有 FAIL / 未满足 expected_criteria |
| 2 | USAGE         | —                | term 词表外 / profile 不合法 / 缺 oracle（正样本强制）/ 目标不在白名单 / 参数冲突 |
| 3 | INCONCLUSIVE  | NO_VALIDATION    | 判据 0 条（KB 侧） |
| 3 | INCONCLUSIVE  | UNREADABLE       | 判据要求的观察项在证据里不存在（"没看到"≠"不满足"） |
| 3 | INCONCLUSIVE  | BUDGET           | `stopped_code == "budget"` |
| 3 | INCONCLUSIVE  | CONNECTION_LOST  | 连接/超时/沙箱异常导致证据不完整 |
| 3 | INCONCLUSIVE  | ORACLE_FAILED    | 扫描前 oracle 预检失败（正样本洞不可复现 → 修靶场，不修 skill） |

负样本（`expects_vuln: false`）才使用 `NOT_VULNERABLE`；找不足流水线禁止此类 profile 进入 §7 的 B 动作。

### 3.3 两条硬规则怎么落到代码里

**规则一：无 validation 时禁止返回 0。**

不是文档承诺，是签名约束：

```python
def confirm(
    criteria: list[Criterion],
    verdicts: list[CriterionVerdict],
    evidence: list[Evidence],
    *,
    expects_vuln: bool,
    surface_hit: bool,
    blocked: bool,
    technique_miss: bool,
) -> Result:
    if not criteria:                       # 空判据永远到不了 0
        return Result(ExitCode.INCONCLUSIVE, SubReason.NO_VALIDATION)
    if blocked:
        return Result(ExitCode.NEGATIVE, SubReason.BLOCKED)
    if expects_vuln and not surface_hit:
        return Result(ExitCode.NEGATIVE, SubReason.MISS_SURFACE)
    if technique_miss:
        return Result(ExitCode.NEGATIVE, SubReason.TECHNIQUE_MISS)
    required = [v for v in verdicts if v.required]
    if any(v.result == "UNOBSERVED" for v in required):
        return Result(ExitCode.INCONCLUSIVE, SubReason.UNREADABLE)
    if any(v.result == "FAIL" for v in required):
        return Result(ExitCode.NEGATIVE, SubReason.FALSE_NEGATIVE)
    if not all(v.passed and v.evidence_refs for v in required):
        return Result(ExitCode.INCONCLUSIVE, SubReason.UNREADABLE)
    return Result(ExitCode.OK)             # 全仓库唯一构造 OK 的路径
```

`Result(ExitCode.OK)` 只在这一个函数里构造，全仓库仅此一处；其它路径想返回 0 必须过这道门。oracle 预检失败在 `confirm()` 之外提前返回 `ORACLE_FAILED`。

**规则二：返回 0 必须打印所依据的判据原文。**

`Criterion.text` 保存一字不改的原文（含编号），打印时直接回显：

```
CONFIRMED  idor   判据 5 条：4 通过 / 1 未观察   证据 3 件   run=ben_20261119_101500_ab12cd34
  #2 通过  "Replacing a numeric id with another account's id returns that account's object"
          ← http/002_GET_api_users_102.req + .resp   (200, body 含 "alice@example.com")
  #4 通过  "Cross-tenant object access returns data outside the caller's tenant"
          ← http/003_GET_api_orgs_77_projects.req + .resp   (200, 4 条跨租户记录)
  #5 未观察  "Provide reproducible steps and evidence"
```

`#5` 这类"过程要求"不会被当成判据参与判定（`is_checkable()` 返回 False，见 §6.2），但仍原样列出，避免"少打印一条"。

**BLOCKED / MISS_SURFACE / TECHNIQUE_MISS 不得降级为 FALSE_NEGATIVE** 同样靠代码保证：正样本下四个 1 的子原因是**有序 if-else**，BLOCKED → MISS_SURFACE → TECHNIQUE_MISS → FALSE_NEGATIVE，且没有任何 `except`/fallback 会把前三者改写为后者。ORACLE_FAILED 在扫描启动前返回，根本进不了这张表。

---

## 4. 数据契约

### 4.1 Profile（靶场描述）

```
Ben/kb/profiles/<name>.yaml        # 随仓库内置
~/.ben/kb-profiles/<name>.yaml     # 用户级，同名覆盖（沿用 ~/.ben/keypool.json 的惯例）
```

```yaml
name: local-idor-positive
description: 本地 docker 靶场；已确认存在 IDOR（正样本）
expects_vuln: true                # 默认 true；找不足时必须为 true。false = 负样本（本方案不用）
targets:
  - http://127.0.0.1:8080
allowed_vulns:                    # 白名单，防"用 A 靶场验 B 类型"
  - idor
defaults:
  max_budget_usd: 0.30
  max_turns: 10
  minutes: 8
baseline:                         # 健康路径：判定 TECHNIQUE_MISS / 靶场存活
  - path: /
    expect_status: 200
# 正样本强制：最小已知可复现请求（黄金路径）。不含凭据字面量。
oracle:
  method: GET
  path: /api/users/102            # 相对 targets[0]
  account_ref: bob                # 用非属主身份发；凭据从密钥文件解析
  expect_status: 200
  expect_body_regex: "alice@example.com"   # 机械断言：跨账号数据出现
  notes: "bob 读 alice 的 user 对象；人工已确认稳定"
# 可选：本靶场上必须 PASS 的 Validation 序号（相对 ## Validation 段内 1-based）
# 未列出的 checkable 判据仍记录，但不阻塞 CONFIRMED（避免过程句拖死正样本）
expected_criteria: [1, 2]
accounts:                         # 跨身份判据用；只存引用名，不存凭据
  - ref: alice
  - ref: bob
```

**正样本硬约束**：

- `expects_vuln: true` 时 `oracle` **必填**；缺字段 → verify 直接 `2 USAGE`，不启动扫描。
- 扫描**之前**跑 oracle 预检（用密钥文件解出 `account_ref`，发与 oracle 同构的请求）：失败 → `3 ORACLE_FAILED`，**不修 skill**。
- `expected_criteria` 省略时退回「全部 checkable 判据 PASS」；正样本建议显式列出，避免过程句（如 idor `#5 Provide reproducible steps`）把 CONFIRMED 卡死。

**凭据绝不入库。** `accounts[].ref` / `oracle.account_ref` 只是名字，实际凭据从 `.env` / `~/.ben/` 读（`Ben/utils/secret_files.py` 已有的私密文件读取路径），profile 里出现 `Cookie:` / `Authorization:` 字面量时 verify 直接报 2 并指出行号。

### 4.2 证据目录

```
_artifacts/kb_evidence/<YYYYMMDD_HHMMSS>_<term>_<profile>/
  verdict.json        # 最终结论（退出码 / 子原因 / 判据 / 证据引用 / 推荐动作）
  criteria.json       # 判据原文 + 逐条判定 + 证据引用（可单独复核）
  oracle/             # 扫描前预检报文（正样本）
    precheck.req
    precheck.resp
  http/               # 扫描期原始报文，一对 .req/.resp
    001_GET_/api/users.req
    001_GET_/api/users.resp
  scan.json           # verify 调起的 run 指针：{run_name, report_path, stopped_code}
```

`_artifacts/` 已在 `.gitignore` 中，**无需新增忽略项**。`scan.json` 只存指针不复制：扫描产物（`_artifacts/runs/<run>`、`_artifacts/reports/report_*.md`）是既有 `run_vuln` 链路的产物，不属于 verify 新增的写点。


> **D2 已锁定**：verify 自身**只新增** `_artifacts/kb_evidence/`（含 `oracle/`）一个写点，扫描产物沿用既有契约并以 `run_name` 引用；`--mode probe` 保留为字面合规路径（S6）。

### 4.3 `verdict.json`

```json
{
  "schema": "ben.kb.verdict/1",
  "term": "idor",
  "resolved": "vulnerabilities/idor",
  "profile": "local-idor-positive",
  "expects_vuln": true,
  "target": "http://127.0.0.1:8080",
  "code": 0,
  "verdict": "CONFIRMED",
  "subreason": null,
  "oracle": {"ok": true, "path": "/api/users/102", "precheck": "oracle/precheck.req"},
  "surface_hit": true,
  "evidence_dir": "_artifacts/kb_evidence/20261119_101500_idor_local-idor-positive",
  "scan": {"run_name": "ben_20261119_101500_ab12cd34", "stopped_code": "finished"},
  "criteria_total": 5,
  "criteria_expected": [1, 2],
  "criteria_passed": 2,
  "criteria_unobserved": 0,
  "criteria": [
    {"index": 1, "text": "Demonstrate access to an object not owned ...", "kind": "numbered",
     "checkable": true, "required": true, "result": "PASS", "by": "mechanical",
     "evidence": ["http/002_GET_api_users_102.req", "http/002_GET_api_users_102.resp"]},
    {"index": 2, "text": "Show the same request fails with ...", "kind": "numbered",
     "checkable": true, "required": true, "result": "PASS", "by": "mechanical",
     "evidence": ["http/003_GET_api_users_102_owner.req", "http/003_GET_api_users_102_owner.resp"]},
    {"index": 5, "text": "Provide reproducible steps and evidence", "kind": "numbered",
     "checkable": false, "required": false, "result": "SKIPPED", "by": null, "evidence": []}
  ],
  "action": {"class": null, "kb_file": null, "hint": null},
  "variants": 3,
  "elapsed_seconds": 41.2
}
```

`action` 由 §7 的 `recommend()` 填充，值域 `"A" | "B" | "C" | "D" | null`。这样 CI 拿到 JSON 就能直接开 issue，不用回查文档。

### 4.4 人读输出统一格式

三条命令共用 `Ben/kb/report.py: emit(result, as_json)`：

- 结果行打 stdout，用法/环境错误打 stderr
- `--json` 时 stdout 只有一份 JSON，其它全部走 stderr（否则 `| jq` 会炸）
- 退出码由 `Result.code` 决定，`main()` 里 `sys.exit(main())` 原样透传

---

## 5. 证据从哪来（v1 稿最大的假设错误）

v1 假定"从 trace 抽请求/响应"。实测不成立：`Ben/core/runner.py:_persist` 只写 root 的 `tool_invocations`，且 `output[:2000]` 截断；`vuln` 聚焦模式下探测动作发生在子 agent，root 的 `invocations` 里基本没有报文。

本方案的证据阶梯（有序，取到即停）：

| 级 | 来源 | 取法 | 可信度 |
|---|---|---|---|
| E1 | Caido 代理 | `Ben/tools/proxy/caido_api.get_request_with_client(id)`，按 `vulnerabilities.json` 的 `http_exchange_ids` 取原始报文 | 原始报文，可作 CONFIRMED 依据 |
| E2 | 沙箱内自采 | verify 在聚焦指令里要求"每次判定性请求把请求/响应写到 `/workspace/.evidence/`"，跑完由 runner 的 workspace 收集侧拷出 | 原始报文（需新增回传，见 S4） |
| E3 | 报告字段 | `vulnerabilities.json` 的 `poc` / `detail` 里的报文片段 | **仅辅助**，不得单独支撑 CONFIRMED |
| E4 | trace | 上面都拿不到时兜底 | 仅辅助，且标记 `source=trace` |
| E0 | 对话落盘 transcript（见 §14） | `_artifacts/runs/<run>/audit/events.jsonl`，按 agent/轮次取 | **仅诊断**：回答「agent 看到/想过/调过什么」，**不得支撑 CONFIRMED**（它不是报文） |

E0 **不参与**上面的「有序，取到即停」——它只在 `MISS_SURFACE` / `TECHNIQUE_MISS` / `FALSE_NEGATIVE` / `BLOCKED` 的诊断输出里使用（用法见 `docs/transcript-audit-plan.md` §5.2），永不作为 `Evidence` 传入 `confirm()`。

**硬约束**：CONFIRMED 要求每条被引用的判据至少绑 1 条 E1/E2 级证据；只有 E3/E4 时返回 3 `UNREADABLE`。这条与"无 validation 禁 0"是同一件事的两个面：**没看到原始报文，就不算看到**。

E2 需要一个小改动：`Ben/core/runner.py` 在 run 结束后把工作区 `.evidence/` 目录拷到 run 目录（只在 `BEN_KB_EVIDENCE=1` 时启用，不影响常规扫描）。

---

## 6. 判定算法

### 6.1 check：可达性

```python
def check(term: str | None, strict: bool) -> Result:
    disk   = {p.stem for p in VULN_DIR.glob("*.md")}      # L_disk
    loaded = {s.name for s in load_skills("vulnerabilities")}  # L_load
    vocab  = Vocab.load()                                  # L_vocab

    if term is None:
        rows = [(name, reachable(name, disk, loaded)) for name in sorted(loaded)]
    else:
        target = vocab.canonical(term)      # None 表示解析不出
        rows = [(term, target in loaded if target else False)]
    if strict:
        rows = [(n, ok and structure_ok(n)) for n, ok in rows]
    return Result(ExitCode.OK if all(ok for _, ok in rows) else ExitCode.NEGATIVE, rows)
```

要点：

- **先查表再 resolve。** `resolve_skill_name` 对未注册名会 `logger.warning` 到 stderr，用户看到的是噪声而不是原因（`vuln.py:166-168` 已踩过这个坑，注释保留）。`vocab.canonical()` 必须在本地表上做，只有确认命中才调 `resolve_skill_name`。
- `L_disk` 与 `L_load` 都要算。只看 `L_load` 会把"文件在但被丢弃"误判成词表缺口，把 diagnose 引向错误归因。
- 可达 ≠ 判据可用。可达只保证"这段知识能进 prompt"，判据可用性是 diagnose/verify 的事。

### 6.2 判据抽取（`Ben/kb/criteria.py`）

```python
@dataclass(frozen=True)
class Criterion:
    index: int      # 段内序号（从 1 起，与原文编号无关，避免原文重排时错位）
    text: str       # 原文，一字不改（返回 0 时要打印）
    kind: str       # "numbered" | "bullet" | "prose"
    checkable: bool # L1 启发式

def extract(body: str, heading: str) -> list[Criterion]   # heading 支持多别名
def is_checkable(c: Criterion) -> bool                    # 有动词 + 可观察对象，且非"注意/小心/避免"
```

段落标题白名单（必须，否则 15 条误判）：

| 用途 | 标题 |
|---|---|
| 判据 | `Validation` |
| 反例 | `False Positives` |
| 绕过节 | `Bypass Techniques`、`Bypass Methods` |

`is_checkable()` 是启发式，只影响 diagnose 的 3 号归因和 verify 的"跳过判定"，**不作为准入闸门**。`idor.md` 第 5 条 `Provide reproducible steps and evidence` 会被判 False —— 它是输出格式要求，不该参与确认判定，但也不删原文（对 agent 仍是有效指令）。误判就修 `is_checkable`，不许改判据文案去迁就校验器。

### 6.3 diagnose：三源求差

```
canonical(term) ─┬─ None ──→ 变体里有能解析的？ ── 是 ──→ 0 叫法缺口
                 │                              └─ 否 ──→ 0 叫法缺口（词表外；变体<2 时按 D3 处理）
                 └─ target ─→ target ∈ L_load ？
                              ├─ 否 ─→ stem ∈ L_disk ？ ── 是 ─→ 1 内容缺口（loader 丢弃）
                              │                        └─ 否 ─→ 1 内容缺口（条目不存在）
                              └─ 是 ─→ Validation 条目？
                                       ├─ 无 ────────→ 1 内容缺口
                                       ├─ 有但全不可核对 → 3 无法归因
                                       └─ 有可核对 ───→ 可达（check 应为 0；若 check 给 1 说明两命令不一致，报 bug）
```

最后一行是自检：三个命令对同一 term 的结论必须自洽，不自洽就是校验器 bug，不是 KB bug。用测试固定这条不变量。

### 6.4 verify：判定分工（机械优先，模型不得单独产生 0）

| 层 | 谁判 | 能得出 | 否决权 |
|---|---|---|---|
| 机械断言 | `verdict.py` 纯函数（status / body regex / header 存在性 / 两侧 diff） | PASS / FAIL / UNOBSERVED | 有：PASS 才能进 CONFIRMED |
| 语义判定 | 一个短生命周期的判定调用（复用模型分层里的子节点） | SUPPORTED / REFUTED / UNSURE，**必须带 evidence_ref** | 无：只能在没有机械断言时把判据标为"观察到"，拿不到 ref 一律降级 UNOBSERVED |
| 汇总 | `confirm()` | 退出码 + 子原因 | — |

判定次序（正样本 `expects_vuln: true`；有序，先命中先返回）：

```
0. oracle 预检失败（扫描前）     → 3 ORACLE_FAILED
1. 判据为空                      → 3 NO_VALIDATION
2. 有 BLOCKED 特征               → 1 BLOCKED
3. surface_hit == false          → 1 MISS_SURFACE
   （oracle 预检已通过，但扫描证据里从未出现 oracle.path）
4. 有 TECHNIQUE_MISS 特征        → 1 TECHNIQUE_MISS
   （打到面 / 基线可达，但 4xx 或与 oracle.expect_* 不符）
5. 必需判据存在 UNOBSERVED       → 3 UNREADABLE
6. 必需判据有 FAIL               → 1 FALSE_NEGATIVE
7. 必需判据全部 PASS 且证据齐    → 0 CONFIRMED
8. 其余                          → 3 INCONCLUSIVE
```

第 5 条排在第 6 条之前是有意的：**"没看到"优先于"不满足"**。反过来的话，一次静默吞包会被记成 skill 假阴性，接着触发 §7 的 B 类改动，把正确的内容改坏。

`surface_hit` 定义：证据目录 `http/*.req`（或 E1 原始报文）中，至少一条请求的 path（去 query 后）等于 `oracle.path`，或 method+path 同时匹配。只看 agent 扫描期报文，**不算** `oracle/precheck.*`。

---

## 7. 结果 → KB 动作（只有 4 类，成本 C < D < A < B）

### 7.1 映射表（正样本：`expects_vuln: true`）

| 触发 | 类 | 改哪里（本仓库真实路径） | 判定依据 | 成本 |
|---|---|---|---|---|
| verify 2 词表外 | **D 扩词表** | `Ben/kb/vocab.py:ALIASES` 新增 `term → 规范名`；`tests/kb/test_vocab.py` 加一条 | term 无法解析，verify 因此 2 | 小 |
| verify 0 且 check 1 | **C 补别名** | `Ben/skills/vulnerabilities/<name>.md` frontmatter 加 `aliases: [...]` | 靶场确认了，但术语取不到 | 小 |
| verify 3 UNREADABLE | **A 改判据** | 同文件 `## Validation` 条目改成可观察断言；若解析器抽不出 → `Ben/kb/criteria.py`（**代码动作，不计入 KB 迭代账**） | 判据要求的观察项在证据里不存在 | 小 |
| verify 3 NO_VALIDATION | **A 补判据** | 同文件新增 `## Validation`（≥3 条编号条目）+ `## False Positives` | 段缺失或为空 | 中 |
| verify 1 MISS_SURFACE | **B 修侦察/入口** | 同文件 `## Reconnaissance` / `## Attack Surface` / `## High-Value Targets`：补 oracle 所在面、如何发现该路径 | oracle 通，agent 从未请求该 path | 中 |
| verify 1 TECHNIQUE_MISS | **B 修手法** | 同文件 `## Testing Methodology` / `## Key Vulnerabilities`：路径·参数·payload 假设对齐 oracle | 打到面但仍 4xx / 与 oracle.expect_* 不符 | 小 |
| verify 1 BLOCKED 且条目无绕过节 | **B 补绕过** | 同文件 `## Bypass Techniques`（或 `Bypass Methods`）补一条 | 403/WAF 特征命中，且绕过节白名单下无条目 | 中 |
| verify 1 FALSE_NEGATIVE 且 check 0 | **B 修内容** | 同文件方法论/关键漏洞段：正样本有洞但判据 FAIL —— **修现有 skill，禁止新建条目** | 打到面、无拦截、必需判据 FAIL | 中 |

**禁止**：正样本上把 `FALSE_NEGATIVE` 解释成「目标没洞」并新建 skill（旧表那一行已废除）。负样本（`expects_vuln: false`）不进入找不足流水线。

**不动 KB**（必须显式记录，否则会有人"顺手改一下"）：

- `verify 0 且 check 0`：闭环，只追加一行迭代日志
- `verify 2`（profile 不合法 / 缺 oracle / 目标不在白名单）
- `verify 3 BUDGET` / `CONNECTION_LOST` / `ORACLE_FAILED`：环境或靶场问题，修环境不修 KB

### 7.2 为什么 C 落在 frontmatter 而不是 description

契约原文说 C 改 `tags / description / title`。本项目三者都不成立：

- 没有 `tags` / `title` 键，`Skill` dataclass 只有 `name` / `description` / `category` / `is_template` / `body` / `source`
- `description` 在目录里被截到 40 字符（`_CATALOG_DESCRIPTION_MAX`），塞进去的别名会被截掉
- 改 `description` 会进 system prompt 的 `<available_skills>`，等于给每次扫描加 token，而别名对 agent 毫无价值

所以 C 的正确载体是新增 `aliases:` 键：`_parse_skill_file` 只解析、`Skill` 加一个 `aliases: tuple[str, ...]` 字段、`skill_catalog()` 不渲染它 → **零 prompt 影响、零 token 成本**。D 与 C 因此也天然分开：D 改 CLI 词表（`vocab.py` 常量），C 改 KB 条目元数据（`.md` frontmatter）。这正好对上"成本 C < D"：C 是加一行 YAML，D 要改代码常量并保证无悬挂词。

### 7.3 动作推荐内建为代码

`Ben/kb/actions.py`：

```python
def recommend(
    *,
    check_code,
    verify_code,
    subreason,
    has_bypass,
    expects_vuln: bool = True,
    strict: bool = False,
) -> Action | None
```

`--json` 输出 `action.class` / `action.kb_file` / `action.hint`，人读输出末行打印同一信息。这样 §7.1 的表**只有一处定义**，文档与行为不会漂移；将来新增子原因，改 `recommend()` 就会在测试里立刻暴露。`expects_vuln=False` 时 `recommend` 对码 `1` **一律返回 `None`**（找不足流水线不消费负样本结论）。

迭代纪律沿用 v1，不做改变：

- **一次迭代只允许一个动作**
- "之后"栏填了才允许提交；未复跑视为未完成
- 记录表：`| 日期 | target | vuln | subreason | verify前 | diagnose | 动作类 | 改动文件 | verify后 | check后 |`
- 每次动作后复跑 `ben skill check <term>` 与对应 `diagnose`，且 `ben skill check --all --strict` 必须仍全绿
- 优先消化 `MISS_SURFACE` → `TECHNIQUE_MISS` → `BLOCKED` → `FALSE_NEGATIVE`（先保证摸到面，再改手法）

---

## 8. 模块结构与依赖方向

```
Ben/kb/__init__.py        对外 API：check_term / diagnose_term / verify_term
Ben/kb/contract.py        ExitCode / SubReason / Result / Criterion / CriterionVerdict / Evidence
Ben/kb/vocab.py           ALIASES（从 vuln.py 迁入）+ canonical() + audit()
Ben/kb/inventory.py       三源清单：disk / loaded / vocab（纯读，无网络无写盘）
Ben/kb/schema.py          L0/L1 结构校验，返回缺失项列表（不抛异常，便于批量）
Ben/kb/criteria.py        Validation / False Positives / Bypass* 抽取 + is_checkable()
Ben/kb/attribution.py     diagnose 归因（纯函数，文件内容以参数注入）
Ben/kb/profile.py         profile 加载 + 双闸校验 + 凭据字面量检测 + oracle/expected_criteria schema
Ben/kb/oracle.py          正样本预检（发请求、落盘 oracle/、返回 ok/ORACLE_FAILED）
Ben/kb/evidence.py        证据采集（E1/E2）+ surface_hit(oracle.path) + 落盘 + sha256
Ben/kb/verdict.py         逐条判定 + 有序汇总（confirm()；正样本子原因表）
Ben/kb/actions.py         结果 → 动作类（§7.1 的唯一实现）
Ben/kb/report.py          emit(result, as_json)：人读 / JSON 统一出口
Ben/interface/cli/skill.py  run_skill(argv)：三个 sub-parser + 分发

Ben/kb/profiles/*.yaml    内置正样本靶场描述（须含 expects_vuln + oracle）
tests/kb/                 单元测试（schema / criteria / vocab / attribution / verdict / actions）
tests/interface/          test_skill_cli.py（三命令的退出码矩阵）
docs/kb-iterations.md     迭代记录
_artifacts/kb_evidence/   运行时产出（已 gitignore）
```

依赖方向严格单向：

```
interface/cli/skill.py → kb/__init__ → {vocab, inventory, schema, criteria, attribution,
                                        profile, oracle, evidence, verdict, actions, report}
                                    ↘ Ben/skills（载入表）        ↘ Ben/config（白名单）
kb/verdict.py → Ben/interface/cli/vuln.py（**仅函数内延迟导入**，避免 vuln → kb.vocab → kb.verify 的环）
kb/oracle.py  → 仅 verify 路径调用；check/diagnose 的 layering 测试禁止导入它
```

三层不越界做成可执行约束，不靠自觉：

- `tests/kb/test_layering.py`：AST 扫描 `check` / `diagnose` 的调用链模块，断言不出现 `requests` / `httpx` / `aiohttp` / `Ben.tools.proxy` 的导入
- 同文件：monkeypatch `Path.write_text` / `Path.mkdir` / `open("w")`，断言 check / diagnose 全程零调用
- `verify` 侧反向断言：除 `evidence_dir` 外的写点为零（`_artifacts/runs` 的写由 `run_vuln` 既有路径豁免，用白名单显式列出）

---

## 9. 分期实施

### S1 词表迁出（纯搬运，最小风险）

- `_ALIASES` 从 `vuln.py:58` 搬到 `Ben/kb/vocab.py`，`vuln.py` 改 `from Ben.kb.vocab import ALIASES`
- `canonical(term)` 查找顺序：`ALIASES` → frontmatter `aliases` → 规范名精确 → 归一化（大小写 / `_`↔`-`）→ stem 变体 → `None`
- `vocab.audit()` 报三类问题：孤词（alias 指向不存在的 skill）、悬挂（skill 无任何叫法覆盖）、歧义（一个 alias 指向多个）
- 验收：`ben vuln --list` 输出与改动前**逐字节一致**；`ben vuln sqli --dry-run`、`ben vuln path-traversal --dry-run` 仍能解析


### S2 `ben skill check`

- `inventory.py` + `schema.py` + `cli/skill.py` + `main.py` 分支 + `completions.py`
- 验收：29 条 `--all --strict` 全绿；人为造 3 个坏样本（缺 Validation 段 / 空段 / 坏 YAML 被丢弃）分别得 1 / 1 / 1，且"坏 YAML"那条的提示指向"loader 丢弃"而不是"词表外"

### S3 `ben skill diagnose`

- `criteria.py` + `attribution.py` + `actions.py`（先只接 diagnose 的 C/D 两类）
- 验收：`sql_injection` → 0（文件名词）、`sql-injection` → 可达、`csrf` → 按 D3 处理（标 `variants:1`）、人造散文式样本 → 3、坏 frontmatter → 2；并跑 §6.3 的自洽性不变量测试

### S4 `ben skill verify` 单类正样本打通

- `profile.py`（`expects_vuln` / `oracle` / `expected_criteria`）+ `oracle.py` + `evidence.py`（含 `surface_hit`）+ `verdict.py`（正样本子原因表）+ `actions.py` 接 verify
- 内置至少 **1** 个正样本 profile（建议先 `idor` 或靶场已确认的那一类），含可跑通的 oracle
- `Ben/core/runner.py`：加 `stopped_code` 枚举（finished / max_turns / budget / error / interrupted / stalled），与 `stopped_reason` 并存不替换
- `Ben/core/runner.py`：`BEN_KB_EVIDENCE=1` 时把工作区 `.evidence/` 拷到 run 目录（E2 通道）
- `Ben/interface/main.py`：`_run_task` 抽出 `_execute_task(args) -> (exit_code, outcome)`，`_run_task` 变薄壳 —— 这是**本方案唯一需要的既有代码重构**，目的是让 verify 拿到 `result.stopped_reason` / `stopped_code` 而不去解析 stdout
- 验收（假 runner，无需靶场）：`NO_VALIDATION` / `ORACLE_FAILED` / `MISS_SURFACE` / `TECHNIQUE_MISS` / `UNREADABLE` / `FALSE_NEGATIVE` 退出码与子原因正确；`BLOCKED` 不会被改写成 `FALSE_NEGATIVE`
- 验收（真靶场）：oracle 预检绿 → 实跑一次；CONFIRMED 时能指着报文说清 `expected_criteria`；人为让 agent 指令避开 oracle.path 时得 `MISS_SURFACE`

### S4b 对话落盘接通 verify（详见 `docs/transcript-audit-plan.md`）

- `Ben/core/audit.py`（writer / hooks / renderer）+ `Ben/core/hooks.py: CompositeHooks` + `runner.py` 三处接线（开关默认关）
- `Ben/interface/cli/transcript.py`：`ben transcript <run> [--agent] [--turn] [--md] [--redact]`
- verify 起扫描前置 `BEN_AUDIT=1`；`verdict.json` 增 `transcript` 段；四个子原因各多打印一句归因（路径差集 / 手法差 / 放弃时的自述 / 拦截页轮次）
- 验收：假模型 2 轮 run 的 `events.jsonl` 事件序列完整；钩子故意抛错不影响 run 完成；`MISS_SURFACE` 输出含 `paths_seen` 与 oracle 差集；`BEN_AUDIT=0` 全程零写盘

### S5 真实假阴性回补

- 按 §7 对一次真实 `MISS_SURFACE` 或 `TECHNIQUE_MISS` 做**一个**动作，写 `docs/kb-iterations.md` 一行（含 subreason），复跑 check + diagnose + verify

### S6（可选）`--mode probe`

- 零模型零写盘的纯探针模式，把"结构可探"判据（状态码 / 响应头存在性 / 路径可达性）机械化；也可只做 oracle 预检回归
- 前置条件：S4 积累了足够多失败样本，能证明哪些判据值得机械化。**不预研**，等真实需求

---

## 10. 验收清单

- [ ] `ben skill check --all --strict` 对 29 条全绿
- [ ] `ben skill check sql_injection` 得 1，`ben skill diagnose sql_injection` 得 0，两者结论自洽
- [ ] `ben skill diagnose` 对 4 个人造坏样本给出 0 / 1 / 2 / 3 正确归因
- [ ] 正样本 profile 缺 `oracle` 时 verify 得 2，不启扫描
- [ ] oracle 预检失败得 3 `ORACLE_FAILED`，且 `recommend()` 返回 `None`
- [ ] `ben skill verify` 的 CONFIRMED 能指着请求/响应说清 `expected_criteria`，且打印判据原文
- [ ] 无判据时 `verify` 不可能返回 0（测试固定）
- [ ] `BLOCKED` / `MISS_SURFACE` / `TECHNIQUE_MISS` 不会被任何路径改写成 `FALSE_NEGATIVE`（测试固定）
- [ ] 正样本上 `FALSE_NEGATIVE` 的推荐动作是「修现有 skill」，绝不是「新建 skill」
- [ ] `check` / `diagnose` 全程零网络、零写盘（测试固定）；且不导入 `kb.oracle`
- [ ] `BEN_AUDIT=0` 时 transcript 零写盘（测试固定）；`verify` 默认开且 `verdict.json.transcript.dir` 可 `ben transcript` 打开
- [ ] `MISS_SURFACE` 的诊断输出能给出 `paths_seen` 与 oracle.path 的差集，不需要人肉翻 agents.db
- [ ] `confirm()` 的 `Evidence` 永不来自 transcript（E0 不得支撑 CONFIRMED，测试固定）
- [ ] `ben vuln --list` 与改造前逐字节一致
- [ ] 词表无孤词（`vocab.audit()` 干净）
- [ ] 迭代记录连续 3 条完成"前 / 后"配对（含 subreason 列）

---

## 11. 风险

1. **`is_checkable()` 必然误判。** 启发式判据只影响 diagnose 的 3 号归因和 verify 的跳过，不影响 check 的绿/红，所以误判不会污染基线。错了就修校验器，**不要为让它通过而改判据文案**——那是把校验器调成永远通过。正样本用 `expected_criteria` 显式收窄必需集，进一步降低过程句干扰。
2. **verify 的语义判定依赖模型。** 判据是英文散文，证据是 HTTP 报文，中间有一次判断。本方案用三条约束压住它：机械断言优先、模型结论必须带 evidence_ref、无 ref 一律降级 UNOBSERVED。oracle 的 `expect_status` / `expect_body_regex` 是机械金标准，优先于语义判定。
3. **22/29 的命名错位是历史包袱。** 方案不重命名文件（会破坏 `git blame` 与外部引用），只用 stem 变体兜住。代价是 `canonical()` 的查找链比理想状态长一步。
4. **E2 通道要改 runner。** `BEN_KB_EVIDENCE=1` 开关保证常规扫描零影响，但仍是本方案唯一触碰 `Ben/core` 的地方，需单独回归（全量失败数基线 99，比较改动前后）。
5. **正样本漂移。** 靶场升级后 oracle 可能失效 → `ORACLE_FAILED`；必须先修靶场/oracle，禁止据此改 skill。S4 实跑依赖至少一个稳定正样本。

---

## 12. 不做

- 自动改 KB（动作只推荐，不改文件）
- 给 29 条 skill 加机器可读的 `validation_checks:` 结构化字段（等真实失败暴露需求再说；现在 151 条编号条目够机读）
- 重命名 `vulnerabilities/*.md` 使其与规范名一致
- 改造 `Ben/rag/`（含 `vector_store.search`、`only`、embedding）
- 动 `Ben/core`（除 `stopped_code` 与 E2 拷贝两处）、`Ben/agents`、`Ben/tools`
- 让 check 承担结构合规的默认职责（`--strict` 才判）
- 自动利用链、横向、报告渲染改造
- 在正样本流水线上用 `NOT_VULNERABLE` / 「新建 skill」解释假阴性
- 无 oracle 的「正样本」profile（缺 oracle = USAGE，不启扫描）
- skill 消融双跑（有/无 skill 对照）——价值高但成本翻倍，等 S5 有假阴性样本后再议

---

## 13. 已锁定事项（D1–D4）

| # | 事项 | 锁定取值 | 备注 |
|---|---|---|---|
| D1 | check 默认判可达还是判结构 | 默认可达，`--strict` 判结构 | 基线守门用 `--all --strict` |
| D2 | verify 写盘口径 | 自身只写证据目录（含 `oracle/`），扫描产物按既有契约以 `run_name` 引用 | `--mode probe` 留 S6 |
| D3 | 变体 < 2 是否一律报 2 | 仅 term 解析失败时报 2 | `--strict-contract` 可恢复原语义 |
| D4 | 模型能否单独产出 CONFIRMED | 不能，必须机械断言（含 oracle.expect_*） | 放宽需先建 golden set |

另增锁定（正样本修订）：

| # | 事项 | 锁定取值 |
|---|---|---|
| D5 | 找不足时 profile 极性 | `expects_vuln: true` + 必填 `oracle` |
| D6 | verify 码 1 语义 | 假阴性；子原因 `BLOCKED` / `MISS_SURFACE` / `TECHNIQUE_MISS` / `FALSE_NEGATIVE` |
| D7 | 实施顺序 | S1 → S2 → S3 → S4（单类正样本）→ S5；不并行铺 29 类 profile |

四项 D1–D4 与 D5–D7 不影响模块划分，**即可进入 S1**。

---

## 14. 增补 v2.1：对话落盘（transcript）

完整方案见 **`docs/transcript-audit-plan.md`**。这里只记对本文件的改动与理由，避免两份文档漂移。

### 14.1 为什么要加这一层

本方案此前的证据链只能回答「**有没有复现**」，回答不了「**为什么没复现**」。三个现成机制都补不上：

- `request_log.py` 明确承诺不记录 prompt/completion 正文（只量大小），是隐私契约；
- `agents.db`（SDK session）没有 system prompt，hooks 每轮注入的告警/frontier 不入库，且会被 compaction 重写——它是续跑存档；
- `trace_*.json` 只有 root 工具调用且 `output[:2000]` 截断，子 agent 报文不入盘。

所以加一个只读旁路：SDK `RunHooks` 上挂 `AuditHooks`，把「模型每轮实际收到的输入 + 输出 + 工具结果 + agent 层级」写进 `_artifacts/runs/<run>/audit/events.jsonl`，结束时渲染人读版。

### 14.2 对本文档的四处影响

1. **§5 证据阶梯**：新增 E0（诊断级，不得支撑 CONFIRMED），不参与「取到即停」。
2. **§9 分期**：新增 `S4b`（在 S4 之后、S5 之前）。其 P1–P4 见落盘方案的 §9。
3. **§10 验收**：新增三条（零写盘、`paths_seen` 差集、E0 不进 `Evidence`）。
4. **默认开关**：只对 `verify` 默认开（`BEN_AUDIT=1`），普通扫描默认关（`0`），保证「不改既有行为」。

### 14.3 新增锁定项

| # | 事项 | 锁定取值 | 理由 |
|---|---|---|---|
| D8 | 记录点 | SDK `RunHooks`（不包 Model、不改 `request_log`） | 按逻辑轮次、自带 agent 身份、不破隐私契约 |
| D9 | 输入落盘方式 | 逐条指纹最长公共前缀差分；`BEN_AUDIT_FULL=1` 强制全量 | 体积 ≈ O(对话总长度)；历史被改写时自动退化，结果始终正确 |
| D10 | transcript 的证据等级 | E0，诊断级，永不进 `confirm()` 的 `Evidence` | 其中的 HTTP 是工具返回值，可能被截断/加工；举证仍只认 E1/E2 |
| D11 | `CompositeHooks` 异常策略 | audit 写失败可吞；`BudgetExceededError` 等控制流异常**必须原样上抛** | 否则预算停扫失效；P1 验收必测 |

D8–D11 不影响 S1–S3 的模块划分，**S4b 可与 S4 并行开发，但必须在 S5 之前合入**——否则第一次真实假阴性回补时，仍然只能靠人肉翻 `agents.db`。
