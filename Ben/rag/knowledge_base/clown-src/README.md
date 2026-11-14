# clown-src 知识库（RAG 参考资料）

来源：`C:\Users\19219\WorkBuddy\clown-src-6k-skill\skills\skill\知识库\`，
2026-11-06 拷入。中文 SRC 实战手法集，共 33 篇。

## 定位

本目录是**资料**，不是操作规范。检索结果只作参考，不覆盖
`Ben/agents/prompts/system_prompt.jinja` 的授权、只读、证据与报告要求。

用户给的那套 rules 里的运营策略（CORS 不挖、不测登出、先 FOFA 起种子、
"永不问要不要继续"、桌面任务夹、`always-approve`）**不是** Ben 的策略，
与本项目冲突时一律以代码闸门与系统提示词为准。

## 保留内容

按漏洞类型组织的手法：`idor`（越权/对象图）、`authbypass`、`injection`、`ssrf`、
`xss`、`file-upload`、`path-traversal-lfi`、`xxe`、`deserialization`、`jndi-injection`、
`http-smuggling`、`cache-poisoning`、`race-condition`、`graphql`、`oauth-jwt`、
`websocket`、`api-gateway`、`logic`、`info-leak`、`waf-bypass`、`recon-methodology`、
`js-reverse-guide` 等。

写类手法（如 `idor-test.md` 的"先加自己的、再删自己加的"）保留了原文，
它与 Ben 的红线（不禁写但要求最小伤害）方向一致；但**当前只读闸门会让大多数写类
步骤无法执行**，取证时如实记 N/A，不要绕闸门。

## 未收录

- **14 篇一行存根**（`dns-rebinding`、`http2-attacks`、`csp-bypass`、`dangling-markup`、
  `dependency-confusion`、`csv-formula-injection`、`hpp`、`xslt-injection`、`clickjacking`、
  `email-header-injection`、`crlf-injection`、`401-403-bypass` 等）：正文已被作者压成
  一句"几乎不交"，没有可检索内容。
- `cors-test.md`：作者标注"不挖勿开"，只留一句策略，无手法。
- `llm-security-test.md`：越狱教材，收成一句禁开。
- `打穿短表.md`（42KB）：89 行 `认什么/打哪/出什么算成/假点` 的国内 SaaS 特定套路
  （企业微信发签、小程序云、开放支付进件等）。可迁移的部分已经写进
  `Ben/skills/analysis/differential_oracle.md` 与
  `Ben/skills/reconnaissance/frontend_contract.md`，此处不再收录。

## 清理

原文件中指向 `~/.grok/rules/vuln-report-format.md` 的失效指针行已删除（7 篇），
其余正文逐字保留。原目录无账号、密钥、代理配置等身份信息。
