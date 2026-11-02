# Security Penetration Test Report

**Generated:** 2026-11-01 13:38:37 UTC

# Executive Summary

对目标 http://117.72.199.2:3000/（OWASP Juice Shop v20.2.0，Angular 前端 + Express 后端）实施了黑盒外部评估。该实例存在多处可被未授权访问的核心缺陷，整体风险姿态为**严重偏高**。

**关键发现**
- **严重**：`POST /rest/user/login` 的 email 字段存在 SQL 注入，未登录即可取得 `admin@juice-sh.op`（id=1、role=admin）的完整 JWT；同一手法可按 id 定向取得任意用户令牌。
- **严重**：`/support/logs/access.log.*` 匿名可下载，日志内嵌完整管理员 RS256 令牌，直接重放即可读取全站 39 个用户的认证明细。
- **高危**：`/rest/products/search` 的 q 参数存在 UNION 注入，一次请求导出全表 40 个用户的邮箱与无盐 MD5 口令哈希。
- **高危**：`/rest/memories` 未授权返回完整 User 对象，含管理员与 deluxe 会员的邮箱、口令哈希与可直接冒用的 deluxeToken。
- **高危**：`/ftp/acquisitions.md` 匿名可读，内容为标注 confidential 的未公开并购计划；同目录 KeePass 口令库与订单 PDF 也可下载。
- **中危**：`/rest/user/security-question` 构成账号存在性判定预言机并泄露密保问题原文。

**业务影响**
客户账号体系被整体突破：管理会话可被任意获取或从日志中拾取，全量用户邮箱与口令哈希外泄，付费会员令牌可被冒用，公司内幕级并购信息与凭据库文件对外公开。同时日志目录暴露了其他客户端的完整请求记录与来源地址。

**修复主题**
输入层缺少参数化查询、认证令牌与敏感字段未做最小化与脱敏、静态目录与日志目录暴露在 Web 根路径之下，三者叠加使单个入口缺陷即可放大为全站失守。

# Methodology

**遵循框架**：OWASP WSTG v4.2 与 PTES 渗透测试执行标准，参考 OWASP API Security Top 10（API1/API2/API5/API8）与 OWASP Top 10 2021（A01/A03/A05/A07）。

**测试类型**：黑盒外部测试，无任何预置凭据，从匿名视角起步。

**范围**：`http://117.72.199.2:3000` 单一主机，应用根路径及其 `/rest/*`、`/api/*`、`/ftp/*`、`/support/*` 等子路径。

**约束**：全程仅执行只读性质的验证性请求；未发起批量爆破、未执行写操作、未触碰范围外地址、未使任何会话失效。

**执行方式**：先以根路径建立基线（状态码、响应体大小、响应头指纹），从 Angular 主 bundle 提取 38 条接口路径形成攻击面清单，将测试拆分为三条并行工作线——注入与认证突破、对象级授权与数据泄露、静态产物与文件目录暴露——各自独立取证后由主控合并校验。

**测试类别**：认证机制与令牌安全性、SQL 注入（登录点与搜索点）、JWT 算法与签名校验、未授权接口访问、对象级授权、敏感信息泄露（日志、备份、配置、目录列举）、静态资源与前端产物分析、安全响应头与信息暴露面。

**关键方法学要点**：因该应用对任意不存在路径统一返回 SPA 首页（200 / 9393 字节），所有「文件存在」判定均与不存在路径的响应摘要做对照，排除软 404 误报；所有注入结论均设有正常请求对照组以确认差分稳定。

# Technical Analysis

**定级模型**：以「可实际达成的未授权或越权后果」为准，而非以缺陷类型计数。严重对应真实取得他人身份或管理会话；高危对应对得上自然人的数据、可用凭证或他人未公开业务正文；中危对应权限扩大面。

**已确认漏洞**

1. **登录接口 SQL 注入致管理员会话失守（严重）** — `POST /rest/user/login` 的 email 直接拼入查询，载荷 `' OR 1=1--` 返回 200 / 784 字节的管理员 JWT，对照组错误口令返回 401 / 26 字节。载荷 `x' OR id=2--` 可按 id 定向取任意用户令牌。根因是认证路径的字符串拼接。

2. **日志目录匿名可读并泄露管理员会话（严重）** — `/support/logs/` 目录可列举，`access.log.2026-11-01` 返回 200 / 36825 字节，内嵌 717 字节管理员 RS256 令牌。重放该令牌后 `/api/Users` 返回 200 / 11275 字节、`/rest/user/authentication-details/` 返回 200 / 14640 字节（39 条记录），已通过「凭证有效性」闸门验证。

3. **搜索接口 UNION 注入导出用户表（高危）** — `GET /rest/products/search?q=')) UNION SELECT id,email,password,'4'...FROM Users--` 返回 200 / 28984 字节，其中 40 行由注入产生，含全量邮箱与 MD5 哈希；正常查询 `q=apple` 为 200 / 921 字节。

4. **回忆墙接口未授权泄露用户对象（高危）** — `GET /rest/memories` 匿名返回 200 / 6183 字节，携带 5 个账号的邮箱、口令哈希与 deluxeToken。同站 `/api/Feedbacks` 对邮箱执行了 `***` 掩码，证明存在输出过滤策略而本接口属覆盖缺口。

5. **并购文件目录未授权读取（高危）** — `GET /ftp/acquisitions.md` 匿名返回 200 / 909 字节未公开并购计划正文；`/ftp/incident-support.kdbx` 返回 200 / 3246 字节口令库文件；`/ftp/` 目录可列举 14 项。

6. **密保问题接口构成账号枚举预言机（中危）** — 存在账号返回 200 / 134 字节含 question 对象，不存在账号返回 200 / 2 字节空对象，差分稳定。

**已排除项（明确控制点）**
- `/rest/basket/{id}`、`/api/Users`、`/api/BasketItems`、`/rest/user/authentication-details/`、`/api/Complaints`、`/api/Cards`、`/api/Addresss` 的匿名访问：统一由应用级 `express-jwt` 中间件在 handler 之前拒绝，返回 401 / 972 字节，经大小写变体与尾斜杠变体验证覆盖全部可达路径。
- 前端主 bundle 硬编码凭据：12 处 secret/privateKey 命中均为代码标识符与模板占位，无 AKIA 前缀、无 PEM 私钥、无硬编码口令。
- `/ftp` 扩展名白名单：`.bak`、`.pyc`、`.yml`、`.gg` 均被 403 拦截，`%00` 变体返回 400。
- 软 404 误报：`/main.js.map`、`/sitemap.xml`、`/swagger.json` 的 200 均为 SPA 首页回退，摘要值与不存在路径一致。

**系统性根因**
- 数据访问层整体缺少参数化查询，注入点在认证与检索两条关键路径上同时出现。
- 敏感对象序列化无统一出口过滤，同站既有掩码策略又存在完全绕过该策略的接口，说明过滤是散点实现而非集中治理。
- 会话凭证的价值未做最小化：JWT 载荷明文携带口令哈希与会员令牌，且被允许出现在 URL 查询串中，最终沉淀进可公开读取的访问日志。
- 静态文件服务与业务应用同域同根，日志、备份、口令库、内幕文档因此直接落入匿名可读面。

**攻击链验证**
已确认可串联的端到端路径：`/support/logs` 读取令牌 → 直接取得管理员会话 → `/api/Users` 与 `/rest/user/authentication-details/` 读取全量用户 → 结合 `/rest/memories` 与搜索注入获取的 MD5 哈希还原口令 → 长期稳定持有多个账号。该链路全程无需任何初始凭据，各环节均已实际打穿并留有可复核证据。

# Recommendations

**立即处置（24 小时内）**
1. `POST /rest/user/login` 与 `GET /rest/products/search` 全部改为参数化查询绑定，禁止字符串拼接进入 SQLite。
2. 将 `/support/logs` 与 `/ftp` 移出 Web 可访问路径，或前置强制认证；关闭静态目录列举（`/ftp/`、`/support/logs/`、`/ftp/quarantine/`）。
3. 轮换全部账号口令与 deluxeToken，重置 `admin@juice-sh.op`、`bjoern.kimminich@gmail.com`、`bjoern@owasp.org`、`ethereum@juice-sh.op` 等已确认暴露的账户。
4. `/rest/memories` 的关联查询改为显式字段选择，禁止整体序列化 User 对象。

**短期（1 周内）**
5. 敏感字段输出层黑名单：`password`、`totpSecret`、`deluxeToken`、`lastLoginIp` 一律不出现在任何响应体中；JWT 载荷只保留 `id` 与 `iat`，角色由服务端按 id 回查。
6. JWT 校验固定算法白名单（仅 RS256），显式拒绝 `alg:none`；校验 `iat`/`exp`/`aud`。
7. 口令哈希由无盐 MD5 迁移至 bcrypt 或 argon2 并加盐。
8. `/rest/user/security-question` 对存在与不存在的账号返回等长同形响应，并加限流与验证码。
9. 修正网关日志：对 `token`、`Authorization`、`Cookie` 等参数落盘前脱敏；令牌禁止出现在 URL 查询串。
10. 登录流程增加失败计数锁定与滑块验证码；下线 `admin123` 等弱口令并强制首登改密。

**中期（1 个月内）**
11. 建立统一的响应体敏感字段过滤器与对象级授权中间件，替代分散在各 handler 内的手工判断。
12. 静态资源与业务应用分离部署，敏感文件放入带签名 URL 的对象存储。
13. `robots.txt` 移除敏感路径标注；`/rest/admin/application-configuration`、`/metrics`、`/api-docs/` 按角色收敛暴露面。

**复测与验证**
修复后按本次报告六个条目的复现步骤逐条回归：登录注入应返回 401 且响应体不含任何 token；`/rest/products/search` 的 UNION 载荷应返回正常空结果；`/rest/memories`、`/ftp/acquisitions.md`、`/support/logs/access.log.2026-11-01` 应返回 401/403 且响应体不泄露任何字段名；`/rest/user/security-question` 对存在与不存在账号的响应长度与结构应完全一致。同时确认历史日志与备份文件已从生产环境中清除，而非仅加访问控制。

