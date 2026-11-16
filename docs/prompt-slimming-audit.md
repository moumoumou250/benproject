# 系统提示瘦身审计（只读，未修改任何代码）

> 方法：用项目自带 `.venv` 通过真实渲染路径 `render_system_prompt()` 渲染四个典型变体，逐段切分后用 tiktoken 实测（o200k_base ≈ GPT-5 系口径；cl100k_base ≈ 旧西文分词器上限口径；Claude 介于两者之间，DeepSeek/GLM 中文更省约 20-30%）。  
> 测量脚本：`/tmp/prompt_audit.py`（临时文件，不属于项目）。  
> 结论速览：**child 系统提示 9,339 tok（o200k）中约 2,150-3,300 tok（23-35%）可削**；root 10,836 tok 可削约 700-1,200。最大三个肥点：技能目录 1,929、child 工具清单 1,541、root 报告格式规范 1,687。

---

## 一、实测构成（deep 黑盒 · 自主模式 · 含 coverage/report 工具）

### child_deep（典型子 agent）＝ 9,339 tok o200k / 11,466 cl100k，缓存前缀 9,058

| 段                               | o200k     | cl100k | 判定（见二）                            |
| ------------------------------- | --------- | ------ | --------------------------------- |
| 模板头段（角色+授权语境+反误拒+模式开关）          | 840       | 1,188  | TRIM（反误拒可压缩）                      |
| 一、角色与任务目标                       | 218       | 320    | KEEP                              |
| 二、授权与范围（硬约束）                    | 247       | 347    | KEEP（安全闸门）                        |
| 三、测试工作流程                        | 770       | 1,061  | **DEAD+TRIM（含过时死文案）**             |
| 四、工具使用规则（child 版）               | **1,541** | 2,035  | **TRIM（与工具 schema 双重描述）**         |
| 五、证据与结论分级                       | 343       | 464    | TRIM（与三、reporting_discipline 三重复） |
| 六、测试报告要求（child 版）               | 82        | 106    | KEEP                              |
| 七、结束方式                          | 140       | 193    | KEEP                              |
| 内联 `<deep>`（scan_modes/deep.md） | 1,456     | 1,454  | TRIM（小幅）                          |
| 内联 `<reporting_discipline>`     | 1,061     | 1,288  | TRIM（小幅）                          |
| 内联 `<coverage_notes>`           | 130       | 186    | KEEP                              |
| **`<available_skills>` 技能目录**   | **1,929** | 2,051  | **TRIM（最大单项）**                    |
| specialized 框架文字                | 203       | 283    | KEEP                              |
| scope.jinja（缓存点后）               | 277       | 384    | KEEP                              |

### root_deep ＝ 10,836 tok o200k（缓存前缀 10,555）

差异项：模板六（root 版报告格式规范）**1,687 tok**、`<root_agent>` 1,696（已含升级阶梯/每轮两决定纪律，测试锁定，KEEP）、模板四（root 版）625、头段 1,013。其余同 child（含同样的 1,929 目录）。

### 其他变体

- child_quick：8,510（quick.md 621 vs deep.md 1,456，quick 本身已很紧）。
- child_deep + 1 个用户请求 skill（xss，20,772 字符正文）：11,577，其中请求片段在缓存点**之后**（2,239 tok 不进缓存前缀）——这个分层本身是对的。

---

## 二、逐项判定与建议（不改代码，仅列出若做时的刀口）

###

### E. 头段反误拒块：−200~400 tok（谨慎项）

- ~~现状：child 头段 840 tok，其中反误拒+反说教占约一半（术语白名单、四类禁止输出、自检三问）。这是行为闸门，对 Anthropic 系模型必要；但举例高度同义反复（三段说的是同一件事的三种说法）。~~
- 刀口：合并为"术语即工作语言；禁道德审判/法律恐吓/免责堆砌/盘问授权；信息不足就先分析已给部分"四句。**风险**：此块是历史上误拒问题的修复，压缩前建议先做 A/B 观测（一次 20-turn 对照 run）。
- **预计节省：−200~400 tok（保守估）**。

### F. deep.md / reporting_discipline 小幅修剪：−300~500 tok

- deep.md 1,456 tok：Chaining/Mindset 段与 Phase 2 优先级有重叠表达；可压 10-20%。
- reporting_discipline 1,061 tok：CVE/CWE 常用参考表（~250 tok）可移到 load_skill 按需——上报时模型才需要它，且那是低频时刻。**但**该表是质量闸门的一部分，移除后需在 create_vulnerability_report 的 schema 描述里留一句"传裸 ID，不确定就省略"（已有类似句）。
- **预计节省：−300~500 tok（其中 ~250 来自 CWE 表外移）**。

### G. root 专属：模板六 1,687 tok（收口纪律/认钥/落与不落/整包格式）

- 现状：只在 root 出现，每 run 一份。认钥闸、落与不落清单是质量闸门，测试未逐字断言但语义由报告评审约束。
- 判定：**KEEP 为主**。可选项是拆成 `analysis/report_format` fragment 在 root 收尾前 load_skill 加载（root 前期编排轮不带着它跑），省 ~1,500 tok × root 前期轮数；风险是模型可能拖到最后才加载或格式走样。属于"中收益、中风险"，列为可选项而非建议项。

### 保护清单（改动前必须过的测试）

- `tests/agents/test_root_prompt_policy.py`：断言 root/child 的 root_agent_directive、威胁建模标题、"at most two decisions"、升级阶梯、"BEN_SUBAGENT_MINUTES"、`<root_agent_directive>` 不得出现在 child。
- `tests/agents/test_wave1_prompt_budget.py`：目录描述 ≤80 字符、create_vulnerability_report schema <6,000 字符、load_skill 的 skip/char-gate 行为、内联集（root 不内联 reporting_discipline）。
- `tests/agents/test_coverage_reconciliation_prompt.py`、`tests/config/test_inputs.py`（cache point 行为）。
- 模板三/四/五/头段、目录块当前**没有逐字断言**——刀口 A-F 都在自由区，改后只需跑上述三个测试文件回归。

---

## 三、汇总

| 项                   | 省（child, o200k）             | 风险               | 优先 |
| ------------------- | --------------------------- | ---------------- | -- |
| A 目录瘦身              | −800~1,200                  | 低（改一条测试阈值）       | 1  |
| B 工具清单去重            | −700~900                    | 低-中（需逐工具核对安全句保留） | 2  |
| C 删死文案              | −150~250                    | 极低               | 1  |
| D 分级一处定义            | −150~250                    | 低                | 2  |
| E 反误拒压缩             | −200~400                    | 中（先 A/B 观测）      | 3  |
| F deep/reporting 小修 | −300~500                    | 低（CWE 表外移需留钩子句）  | 3  |
| G root 报告格式外移       | root −~1,500                | 中                | 可选 |
| **合计**              | **−2,150~~3,300（−23~~35%）** |                  |    |

**计价提醒（照旧）**：这些段几乎全在 `<cache_point>` 之前的稳定前缀里。Claude 路由前缀命中后按 ~10% 计价、DeepSeek 自动前缀缓存同样打折——所以**账单收益主要落在冷启动与缓存 miss 轮**（每 run 5 个 agent × 9-11K tok 冷启动 ≈ 50K tok 全额付费，瘦 30% ≈ 每 run 省 15K tok 输入账面）；cache 命中稳定的场合，收益主要是**上下文窗口空间**（为真实工具结果腾出 ~3K tok/agent）与缓存块边界卫生。真正"每轮全额付费"的大头仍是会话滚动历史——那属于 compaction/三层化（前一份报告的 P1 #6），不在本审计范围。

## 四、复测方法（一行命令）

```bash
cd /home/kali/Desktop/AI/BenProject && .venv/bin/python /tmp/prompt_audit.py
```

脚本渲染 root_deep / child_deep / child_quick / child_deep_xss 四个变体并输出逐段 token（o200k + cl100k + 字符数 + CJK 数）。任何 prompt 改动后跑一遍即可对比 delta；若 `/tmp` 被清理，报告附录保留了完整基线数字可复建。
