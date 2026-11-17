# Schema 瘦身改写稿（可直接落盘）

> 目标文件：`Ben/tools/reporting/tool.py`、`Ben/tools/threat_model/tools.py`
> 全部改动**只碰 docstring**，不动签名、不动校验、不动逻辑。落盘后需重新构建 `FunctionTool` 复测字符数。
> 字符→token 换算：÷3.9（英文技术文本标定值，与 9k / 1.6k 两个锚点一致）。

## 结论

| 范围 | 现状 | 改写后 | 净减 | 比例 |
|---|---:|---:|---:|---:|
| 报告工具 6 件 | 9,435 tok | **6,390 tok** | −3,045 | −32.3% |
| 威胁建模 3 件 | 1,768 tok | **1,299 tok** | −469 | −26.5% |
| 子 Agent 工具面 | 23,511 tok | **19,997 tok** | −3,514 | −14.9% |

比上一版评估的 −41.8% 低 9 个百分点，原因在下面三条硬约束。`test_reporting_fields` 三锁无需改测；**A13 须保留 `orchestrator`/`root agent` 关键词**（见 [`schema-slimming-evaluation-v1.md`](schema-slimming-evaluation-v1.md)），否则 `test_list_reports` 会红。

---

## 一、三条硬约束（决定"能删到哪"）

### 1. `tests/report/test_reporting_fields.py:1240` 锁定格式化指引

```python
assert "markdown" in vuln_desc.lower()
assert "fenced code" in vuln_desc.lower()
```

create 的描述里必须留 `markdown` 与 `fenced code` 两个词。所以不能像上一版那样整段指向 skill，必须保留一行"每个文本字段用 markdown + 带语言标记的围栏代码块"。

### 2. 同文件 `:1258` 锁定 git blame 提示的存在与位置

```python
assert desc.count("blame") == 1
tail = desc[desc.index("Nice to have:"):]
assert "git blame" in tail and "technical_analysis" in tail
```

它要求：`blame` 只出现一次、"Nice to have:" 之后仍要提到 `git blame` 与 `technical_analysis`。**压成一行仍然通过**——这是我最先想删、后来发现可以保留语义只压缩长度的地方（290 字符 → 130 字符）。

### 3. `create_dependency_report` 的描述要保留 `cve` 与 `reachab`

同文件 `:1254-1255`。压缩后的文本里两者都有。

### 4. 落地时额外发现的锁（规范 v1 原稿未列）

- `tests/interface/test_list_reports.py`：`list_reports` 描述须含 `orchestrator` 或 `root agent`
- `test_update_tool_asks_for_the_report_and_the_reason`：update 描述须含 `not deduplication`
- `test_dep_tool_exposes_contextual_cvss_params`：`contextual_cvss_breakdown` 须含 `source-to-sink`

评估与落地记录见 [`schema-slimming-evaluation-v1.md`](schema-slimming-evaluation-v1.md)。

---

## 二、A 组：`Ben/tools/reporting/tool.py`（12 处）

### A1 ｜ `create_vulnerability_report` 工具描述（1010–1068）

现状 2,805 字符。保留 When / When NOT / Severity gate / 去重 / 一行格式化 / skill 指针 / 一行 git blame；删除"Report output rules"的展开与"Counterevidence pass"整段（后者由 `analysis/counterevidence` 与本字段描述承担）。

```
    File a vulnerability report — one report per fully-verified finding.

    **When to file**: you have a concrete vulnerability with a working PoC
    and you're 100% sure it's real.

    **When NOT to file**: security observations without a specific
    vulnerability; suspicions you haven't confirmed with a PoC; several
    vulnerabilities in one report; something already filed. Known-CVE
    dependency findings go to ``create_dependency_report`` instead.

    **Severity gate**: a reachable endpoint, an unusual response, a weak
    configuration, or a recon artifact is not by itself a vulnerability —
    file only when the PoC demonstrates an unauthorized security
    consequence. Score the impact the PoC supports, never a speculative
    pivot. The impact narrative, the PoC, and every non-None CVSS impact
    metric must describe the same demonstrated consequence; when the
    evidence is incomplete, lower the metric or keep validating.

    Same-root-cause duplicates are rejected automatically — on
    ``duplicate_of``, revise with ``update_vulnerability_report``.

    **Any text field may be rendered into reports**: use markdown in every
    text field, with fenced code blocks for code, payloads and HTTP
    excerpts. ``poc_description`` is steps only; ``remediation_steps`` is
    prose only.

    Full writing / CVSS / CWE / white-box rules:
    ``load_skill("analysis/reporting_discipline")``. Validation errors
    repeat the relevant rule. Nice to have: weave a ``git blame`` of the
    vulnerable line into ``technical_analysis`` when the checkout has
    history.
```

**Δ −1,309 字符（−336 tok）**。正确性：无变化——被删的两段在 `reporting_discipline.md:16` 与 `counterevidence.md:152` 有全文。

### A2 ｜ `counterevidence`（1094–1102）

现状 485 → 206 字符。

```
        counterevidence: The strongest case that this finding is NOT exploitable or is
            less severe, with the evidence you checked. A finding you could not
            execute is at best ``confidence: medium``. See
            ``analysis/counterevidence``.
```

### A3 ｜ `confidence`（1103–1124）

现状 377 → 174 字符。

```
        confidence: ``high``: PoC executed end-to-end with captured evidence.
            ``medium``: partially executed or evidence incomplete. ``low``:
            inference only. Explain in ``confidence_rationale``.
```

### A4 ｜ `code_locations`（1125）——**本次唯一的正确性修复**

现状 46 字符（"White-box findings — list of location objects."）。它真正的规范目前错位在 `http_exchange_ids` 下面（1126–1199 中的 2,697 字符）。把压缩版放回这里：

```
        code_locations: White-box findings: one object per contiguous code block.
            ``file`` (required, relative to repo root, no leading ``/`` or ``..``),
            ``start_line`` / ``end_line`` (required, 1-based, verified against the
            file — never guessed), ``snippet``, ``label``, plus optional
            ``fix_before`` / ``fix_after``, which render as a PR suggestion block
            replacing lines ``start_line``-``end_line``: ``fix_before`` must match
            the source character-for-character and ``fix_after`` must be the
            complete syntactically valid replacement, or the accepted suggestion
            corrupts the code. Split non-contiguous changes into separate entries,
            primary fix first.
```

**Δ +580 字符（+149 tok）**。这是全案唯一"亏本"的改动，接受它换取白盒字段第一次具备约束。

### A5 ｜ `http_exchange_ids`（1126–1199）

现状 3,408 字符，其中 2,697 是上面那段错位的 `code_locations` 规范。**删除 1139–1199 整块**，保留并压缩 ID 规则（3,408 → 313）：

```
        http_exchange_ids: Proxy request IDs that prove this finding, copied from
            ``list_requests`` / ``view_request``. Include baseline/control requests
            as well as the exploit. Omit only when the finding has no captured HTTP
            evidence. Never invent IDs to bypass a verification error. Keep IDs out
            of ``evidence`` and all other report text.
```

**Δ −3,095 字符（−794 tok）**。

### A6 ｜ `fix_verification`（1200–1245）

现状 1,604 → 263。4 道闸门的全文在 `_validate_fix_verification`（`tool.py:335-357`）的报错里逐条复述，描述不必再到带。

```
        fix_verification: Required when any ``code_locations`` entry carries ``fix_after``:
            state that you re-traced the source->sink path through the patched
            code, re-ran the PoC if possible, and reviewed the bypasses. Gates and
            disqualifiers: ``load_skill("analysis/fix_verification")``.
```

**Δ −1,341（−344 tok）**。违规时规则由报错全文返回。

### A7 ｜ `update_vulnerability_report` 工具描述（1320 起至 `Args:` 前）

现状 2,004 → 996。删掉四个"你会遇到什么情况"的叙事场景，保留 4 条字段交互规则（这些规则没有别处承载）。

```
    Revise a filed report, keeping its id. Use it after you build the
    working exploit, chain findings to raise real impact, downgrade
    severity, or fix a wrong field. Call ``list_reports`` / ``get_report``
    first to find the id.

    Pass only the fields to replace; everything else stays as filed.
    Reporting rules of ``create_vulnerability_report`` apply to every field
    you pass.

    - ``cvss_breakdown`` replaces the whole vector — pass all 8 metrics.
      On a dependency finding it replaces the contextual rating and needs
      ``contextual_cvss_reasoning`` with it.
    - A dependency finding never carries ``endpoint``, ``method`` or a PoC.
    - An explainer field with no replacement is dropped when the field it
      explains changes: pass ``confidence_rationale`` with ``confidence``,
      ``severity_change_conditions`` with ``cvss_breakdown``.
    - ``code_locations`` replaces the whole list; a location carrying
      ``fix_after`` needs ``fix_verification``.

    The revision is recorded as update history — state the reason.
```

**Δ −1,008（−258 tok）**。

### A8 ｜ `create_dependency_report` 工具描述（1951 起至 `Args:` 前）

现状 2,345 → 830。删掉"什么时候该用/不该用"的展开与 reachability 五级阶梯全文（阶梯在 `reachability` 参数里仍有枚举，分析流程在 skill 里）。

```
    File a known-CVE dependency (SCA) finding — one report per CVE x
    package. Use this instead of ``create_vulnerability_report`` when the
    finding is a vulnerable third-party package/version identified from a
    lockfile, manifest or SBOM: a verified advisory plus the affected
    installed version is the evidence, no live PoC required.

    Do NOT file outdated-but-not-vulnerable packages, dynamically proven
    vulnerabilities, or the same CVE/package twice.

    Never suppress a finding because the vulnerable path looks
    unreachable — file it and record the analysis in ``reachability`` and
    ``reachability_evidence``. Reachability affects prioritization only,
    never the rating: severity comes from ``contextual_cvss_breakdown``
    when you provide one, otherwise from ``advisory_cvss``.

    Procedure: ``load_skill("custom/dependency_cve_scanning")``.
```

**Δ −1,515（−389 tok）**。

### A9 ｜ dep 的 `contextual_cvss_breakdown`（2075–2098）

现状 1,335 → 441。8 个指标名不重列（与 create 的 `cvss_breakdown` 同一形状，第三份重复），摘要只留"从 advisory 出发、只改 trace 证明不同的"。

```
        contextual_cvss_breakdown: **Required.** Full CVSS v3.1 rating of this CVE **in this codebase**,
            same 8-metric object as ``create_vulnerability_report``'s
            ``cvss_breakdown``; all 8 required when set, and you never supply a
            score. Start from the advisory's published metrics and change only what
            the trace in ``reachability_evidence`` proves differs here. Send it on
            every report: when the trace changes nothing, repeat the advisory
            metrics and say so in the reasoning.
```

**Δ −894（−229 tok）**。

### A10 ｜ dep 的 `reachability_evidence`（2042–2057）

现状 913 → 482。source-to-sink 的**格式**保留（`entry point -> intermediate call -> package call` + 每跳 `file:line`），逐跳推理细节交给 skill。

```
        reachability_evidence: **Required.** Concrete proof for the claimed level: repo-relative
            ``file:line`` of the import or symbol hit, the matched advisory
            symbols, or the govulncheck path. For ``unknown``, say what you
            searched and why it is inconclusive. When the symbol is in use, also
            give the source-to-sink trace as ``entry point -> intermediate call ->
            package call`` with a ``file:line`` per hop, what each hop enforces,
            and who controls the input; state plainly when no entry point reaches
            the sink.
```

**Δ −431（−111 tok）**。

### A11 ｜ dep 的 `contextual_cvss_reasoning`（2099 起）

现状 645 → 247。

```
        contextual_cvss_reasoning: **Required.** Two to four sentences a reviewer can verify without
            opening the repo: how the app uses the package, the ``file:line`` you
            inspected, whether an attacker controls the input reaching the sink,
            and what the adjustment therefore changes.
```

**Δ −398（−102 tok）**。

### A12 ｜ 四个低价值字段

| 字段（行） | 现状 | 改写为 | Δ |
|---|---:|---|---:|
| `advisory_cvss`（2014） | 297 | `**Required.** Published advisory base score (0.0-10.0), read off trivy / NVD / GHSA — never guessed. Rates the finding only when no ``contextual_cvss_breakdown`` is given.` | −126 |
| `introduced_by`（2022） | 237 | `For a transitive dependency, the direct dependency that pulls the vulnerable package in, as ``name@version``. Omit for direct deps.` | −106 |
| `manifest_path`（2031） | 376 | `**Required.** Repo-relative path of the lockfile/manifest where the vulnerable version was observed, with any checkout prefix stripped.` | −241 |
| dep `code_locations`（2058） | 661 | `Same shape as ``create_vulnerability_report``'s ``code_locations``: the import/call site behind your ``reachability`` claim, as repo-relative ``file`` + 1-based ``start_line`` / ``end_line``, with optional ``snippet``, ``label``, ``fix_before`` / ``fix_after``.` | −400 |

合计 **−873 字符（−224 tok）**。

### A13 ｜ `list_reports` 工具描述（2351 起至 `Args:` 前）

现状 1,636 → 529。删掉"For the orchestrator / root agent……如果你是 subagent 请忽略"整段——角色分工属于提示词，不该让每个子 Agent 为它付费。保留返回字段、过滤语义、排序、`include_details` 的代价提示。

```
    List filed reports as metadata: ``id``, ``title``, ``severity``,
    ``cvss``, ``confidence``, ``finding_class``, ``cve``/``cwe``,
    ``target``/``endpoint``, ``fix_effort``, ``agent_name``, ``timestamp``,
    a 280-char ``description_preview``, plus ``total_count`` and
    ``severity_counts``. Filters compose: ``severity`` and
    ``finding_class`` match exactly, ``target`` and ``search`` are
    substrings. Sorted by severity then id. ``include_details=True`` adds
    full bodies (token-expensive) — prefer ``get_report`` for one finding.
    Read-only.
```

**Δ −1,107（−284 tok）**。落地时保留半句 `Primarily for the orchestrator / root agent; subagents prefer get_report for one id.`，以满足 `test_list_reports_docstring_scopes_to_orchestrator`。

---

## 三、B 组：`Ben/tools/threat_model/tools.py`（3 处）

### B1 ｜ `save_threat_model`（2,849 → 1,761）

四段结构与 400 字符门槛本身就是运行时校验（`tools.py:39-58`），描述只需说清"覆盖语义 + 四段是什么"。**顺手修一句事实错误**：原文写"it is not written to disk"，但 `_persist_locked()` 确实写 `{state_dir}/threat_models.json`，只是不跨 run。改写为 "Scoped to this run"。

```
    Share a target-scoped threat model with the other agents on this scan.
    Scoped to this run; a later scan starts without it.

    **Replaces the whole document and clears any amendments.** Use it to
    establish the baseline (normally root, before subagents start), or to
    fold accumulated amendments back into the body. If a model exists and
    you only need to correct or extend part of it, call
    ``amend_threat_model`` instead — saving over it silently discards what
    other agents added.

    Write it from the evidence you have. White-box: name the files,
    entrypoints and controls behind each claim. Black-box: ground it in
    recon — answered hosts and ports, fingerprints, roles and tenants, the
    auth and session model, enumerated endpoints — and say which claims are
    inferred rather than observed. Scope it to the target, not your slice
    of it: do not centre it on the diff you were handed or the one host
    that answered first, and in a monorepo do not let ``tests/`` or one-off
    scripts become the centre of gravity unless the code shows they are
    deployed.

    Markdown, these four sections required:

    - **Overview** — what the target is; product/runtime versus tooling.
    - **Trust Boundaries and Assumptions** — boundaries, the actors on
      either side, the invariants. Separate attacker-, operator- and
      developer-controlled input. Black-box, this is the role/tenant
      privilege model: what is reachable unauthenticated, as a low-privilege
      user, and across tenants.
    - **Attack Surface and Attacker Stories** — exposed surfaces, existing
      mitigations that change severity, realistic stories, and which
      stories are not realistic here and why.
    - **Severity Calibration** — what critical / high / medium / low look
      like for *this* target, with a concrete example at each level.
```

**Δ −1,088（−279 tok）**。

### B2 ｜ `amend_threat_model`（1,351 → 955）

```
    Correct or extend the existing threat model without replacing it.

    The baseline is written before anyone starts hunting, so it holds the
    least information anyone will ever have. When your work contradicts it
    or fills in something it missed, record that here: every agent that
    calls ``get_threat_model`` afterwards sees your addendum next to the
    base text. Amendments are append-only and attributed, so concurrent
    amendments both survive — that is the difference from
    ``save_threat_model``, which overwrites the document and drops them.

    Worth amending: a boundary the model calls trusted that you found
    attacker-reachable (or the reverse); a host, endpoint, parameter, role,
    sink or shared control it omits; something it only inferred that you
    have now observed, or that turned out false; a severity call that is
    wrong for this target, with the reason.

    Not worth amending: individual findings (those are reports) or
    restating what the model already says.
```

**Δ −396（−102 tok）**。

### B3 ｜ `get_threat_model`（1,128 → 786）

删掉与 `_not_found` 返回值重复的劝导语（那段话本来就在返回 JSON 里）。

```
    Read this scan's threat model for a target, if an agent has derived one.

    The model is this run's shared answer to who the attacker is, where the
    trust boundaries sit, and what counts as critical here. Call it before
    you start hunting so you inherit that view instead of re-deriving it.

    Scoped to this run — nothing carries over from an earlier scan. Works
    black-box or white-box: the target can be a host, URL, API base or
    repository path; equivalent spellings of one host resolve to the same
    model, and a checkout resolves to its remote.

    ``found: false`` means nothing has been derived yet. Any
    ``amendments`` in the response are corrections recorded after the base
    model — they are part of the model, so read them and prefer the later
    statement where one contradicts the base text.
```

**Δ −342（−88 tok）**。

---

## 四、执行顺序

单次提交改动面较大，建议按三批走，每批可独立回滚：

| 批次 | 内容 | 理由 |
|---|---|---|
| 1 | A4 + A5（`code_locations` 与 `http_exchange_ids` 互换正确归属） | 纯搬移，`git diff` 应能看出"同一段内容从一处移到另一处"；唯一影响产出质量的修复 |
| 2 | A1、A2、A3、A6（create 描述与其三个字段） | 集中在同一 docstring，一次改完 |
| 3 | A7–A13 + B1–B3 | 纯压缩，逐个工具改，无相互依赖 |

`recon` / `enum` 两个 profile 白名单不含这些工具，`root` 已裁掉 4 个写工具——**改动只影响 `exploit` / `validate` / `default` 的执行类子 Agent**，不需要动 `profiles.py` 或 `factory.py`。

---

## 五、验证

```bash
# 1) 单元测试（影响面：报告字段与工具描述断言）
.venv/bin/python -m pytest tests/report/test_reporting_fields.py -q          # 基线 96 passed
.venv/bin/python -m pytest tests/tools/test_threat_model_tool.py -q

# 2) 复测字符数（期望 24,922 ±5%）
.venv/bin/python - <<'PY'
import json
from Ben.agents import factory as f
REP={"create_vulnerability_report","create_dependency_report","update_vulnerability_report",
     "delete_vulnerability_report","list_reports","get_report"}
TM={"get_threat_model","save_threat_model","amend_threat_model"}
tot=rep=tm=0
for t in f._BASE_TOOLS:
    s=json.dumps({"name":t.name,"description":t.description,"parameters":t.params_json_schema},
                 ensure_ascii=False)
    tot+=len(s)
    if t.name in REP: rep+=len(s)
    if t.name in TM: tm+=len(s)
print("全量", tot, "≈", round(tot/3.9), "tok")
print("报告", rep, "≈", round(rep/3.9), "tok  (目标 24,922 / 6,390)")
print("建模", tm,  "≈", round(tm/3.9),  "tok  (目标 5,068 / 1,299)")
PY
```

**验收标准**

1. `pytest tests/report/test_reporting_fields.py` 仍 96 passed（无需改测试）；
2. 报告类字符数落在 23,600–26,200，威胁建模落在 4,800–5,350；
3. `create_vulnerability_report` 的 `http_exchange_ids` 描述 < 400 字符、`code_locations` > 500 字符（错位已纠正的反向指标）；
4. 手工跑一次白盒上报：只给 `fix_after` 不给 `fix_verification`，确认报错仍返回 4 道闸门全文。

---

## 六、明确不做

1. **不做 `str | None = None` → `str = ""`**。实测只省 358 tok，却要在 `_do_create` / `_collect_update_changes` 里重新定义"空串 = 未提供"，是行为改动。收益与风险不成比例。
2. **不动 `delete_vulnerability_report` 与 `get_report`**（1,079 + 342 = 364 tok 的描述）。前者是操作确认文案，后者是分页说明，都在用。
3. **不删 `Severity Calibration` 段**（威胁建模的 `save` 里）。那是这个模块唯一不可替代的输出，删了它模型就没有存在理由。
4. **不改 `_BASE_TOOLS` 的注册策略**。真正更深的一步是"按 profile 给 `exploit` 挂精简报告面"，但那会让同一个工具在不同 Agent 身上语义不一致，属于架构决策，不适合夹在文案压缩里做。

---

## 附：本稿与上一版评估的差异

| 项 | 上一版（评估） | 本稿（可落盘） | 原因 |
|---|---:|---:|---|
| 报告工具 | 5,489 tok（−41.8%） | 6,390 tok（−32.3%） | 保留 `markdown`/`fenced code`、保留 git blame 一行、`update`/`dep` 描述保留交互规则与 reachability 语义 |
| 威胁建模 | 863 tok（−51%） | 1,299 tok（−26.5%） | 四段结构说明必须留在描述里（它是运行时校验的对象，模型需要知道写哪四段） |
| 是否需改测试 | 需改 2 处 | **不需要** | 新文本已复刻并通过现有断言 |
