# Schema 瘦身改写稿 · 合理性评估（v1）

> 日期：2026-11-17。对象：[`schema-slimming-patch-spec-v1.md`](schema-slimming-patch-spec-v1.md)。  
> 背景测量：[`reporting-vs-threat-schema-token-optimization-v1.md`](reporting-vs-threat-schema-token-optimization-v1.md)。

---

## 结论

**方向合理，值得做。** docstring-only、不动签名/校验/逻辑；报告约 −32%、威胁约 −27%、子 Agent 工具面约 −15%，量级可信。

**有条件批准全文落盘**：优先 A4/A5（`code_locations` 错挂归位）；A13 必须保留 `orchestrator` / `root agent` 关键词（或同步改测），不能宣称「零测试改动」。

| 主张 | 判定 |
|---|---|
| 「落盘后不需要同步任何测试」 | **不成立**（A13 vs `test_list_reports_docstring_scopes_to_orchestrator`） |
| 批次 1「纯搬移」 | **不成立**（归位 + 压缩重写） |
| 「改动只影响 exploit/validate/default」 | 报告写工具成立；B1–B3 影响 **root**（仍挂威胁建模） |

---

## 成立

1. `test_reporting_fields` 三锁（`markdown`/`fenced code`、单次 `blame`+Nice to have、dep `cve`/`reachab`）认得对。
2. `code_locations` 规范错挂在 `http_exchange_ids` 下是真缺陷。
3. §六 不做空串语义、不改注册、不删 Severity Calibration —— 正确。
4. `save_threat_model`「not written to disk」是事实错误；改为 run-scoped 正确。

---

## 风险与执行约定

- A2/A6 把反证 / fix_verification 长文压到 skill+报错 → 监控首轮失败率。
- A3 `high` 语义略收紧（偏端到端 PoC）。
- **落地默认**：A13 保留半句 orchestrator/root；批次 1 按「归位并压缩」验收。
- 规范漏记的额外锁词（落地已补）：`update` 描述须含 `not deduplication`；dep `contextual_cvss_breakdown` 须含 `source-to-sink`。
- 与台账 P0/P1 **正交可并行**。

## 落地结果（2026-11-17）

已改 [`Ben/tools/reporting/tool.py`](../Ben/tools/reporting/tool.py)、[`Ben/tools/threat_model/tools.py`](../Ben/tools/threat_model/tools.py)。

| 验收项 | 结果 |
|---|---|
| `pytest` 相关三文件 | **162 passed** |
| 报告 schema 字符 / tok | 24,920 / ≈6,390（目标区间内） |
| 威胁建模字符 / tok | 5,118 / ≈1,312（目标区间内） |
| 全量 `_BASE_TOOLS` | 77,842 / ≈19,959（目标 ~19,997） |
| `http_exchange_ids` / `code_locations` | 313 / 626（<400 / >500） |

## 验收

```bash
.venv/bin/python -m pytest tests/report/test_reporting_fields.py \
  tests/interface/test_list_reports.py tests/tools/test_threat_model_tool.py -q
```

报告类字符数约 23,600–26,200；威胁建模约 4,800–5,350（÷3.9）。
