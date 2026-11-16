---
name: reporting_discipline
description: 结构化上报纪律——发现即时登记、证据一一对应、最终报告与登记一致；含写作规范与 CVSS/CWE 口径
template: none
---
## 结构化上报纪律

结论分级与收口以系统提示「五、证据与结论分级」为准（已验证 / 疑似·未验证 /
未发现 / 已排除），此处不重复定义。

1. 每确认一项发现立即登记：目标、操作、真实证据、结论分级四要素齐全。
2. 登记内容必须逐字来自工具真实返回，不允许凭记忆补写数字。
3. 最终报告中的每条结论都能在登记记录中找到对应证据，反之亦然。
4. 证据不足的发现按第五部分保留为"疑似/未验证"，不在登记中升级为漏洞。

## 写作与渲染规范（原 create_vulnerability_report 长 description）

- 禁止泄露内部细节：不要写 `/workspace`、内部工具名、agent、sandbox、模型、
  system prompt、内部报错栈，以及 proxy request ID / 内部 report ID。
- 语气：正式、客观、第三人称、厂商中立、简洁。避免 "QUICK" / "Approach" /
  "Techniques" 这类像工程 runbook 的小标题。
- 每个文本字段使用 markdown：`**bold**`、`` `inline code` ``、带语言标记的
  fenced code block。引用源码时在开栏标注
  `title=app.py startLineNumber=42 endLineNumber=50`。
- `poc_description` 只写步骤，代码一律进 `poc_script_code`；
  `remediation_steps` 只写散文，补丁代码进 `code_locations`。
- 仅 PoC 与 Remediation 允许编号步骤；避免含糊措辞。
- 字段结构建议：(1) `description` 概述 (2) `cvss_breakdown` (3) `target`/
  `endpoint` (4) `technical_analysis` (5) `poc_description` +
  `poc_script_code` (6) `impact` (7) `evidence` (8) `remediation_steps`。

## 严重性与 CVSS 口径

`cvss_breakdown` 必须含 8 个指标（各大写单字母）：

- `attack_vector`: N/A/L/P
- `attack_complexity`: L/H
- `privileges_required`: N/L/H
- `user_interaction`: N/R
- `scope`: U/C
- `confidentiality` / `integrity` / `availability`: N/L/H

按**已证明**的攻击路径打分，不要按漏洞类别或扫描模板：

- `C:L` 需真实拿到部分受限信息；纯侦察价值是 `C:N`。`C:H` 需大范围泄露或
  可用的管理员凭证/私钥级影响。
- `I:*` / `A:*` 同理：未证明修改/中断则用 `N`。
- `S:C` 仅当利用确实跨到另一安全权威域；下游名字不等于 Changed Scope。
- 不要预设"攻击者已持有受害者密钥"：会话残留/可重放链接本身通常是 Low/
  Medium 的会话管理问题，除非同一发现证明了获取密钥的路径。
- 需要 MitM 或受害者动作时，如实抬高 `attack_complexity` /
  `user_interaction`，不要假设理想条件恒成立。

## CVE / CWE

- 只传裸 ID（`CVE-2024-1234`、`CWE-89`），不确定就省略。
- 优先最具体的子 CWE；不要用宽泛父类（CWE-74/20/200/284/693 等）。
- 常用参考：CWE-79 XSS、CWE-89 SQLi、CWE-78 OS Command、CWE-94 Code
  Injection、CWE-287/862/863 鉴权、CWE-352 CSRF、CWE-918 SSRF、
  CWE-601 Open Redirect、CWE-434 Upload、CWE-502 Deserialization、
  CWE-22 Path Traversal、CWE-611 XXE、CWE-798 Hard-coded Credentials。

## 白盒 `code_locations`

有源码时必须填。`fix_before`/`fix_after` 是字面 PR suggestion：必须与
`start_line`–`end_line` 原文逐字一致。非连续修改拆成多条 location，用
`label` 标明角色。无 `fix_after` 的条目只作信息性上下文。
路径相对仓库根，禁止 `/workspace` 前缀与 `..`。

有 `fix_after` 时必须写 `fix_verification`：按顺序过安全闭合 → 绕过复查
→ 行为保持 → 仓库检查，并如实记录命令结果与缺口。
