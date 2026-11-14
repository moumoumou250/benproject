---
name: frontend_contract
description: 从前端 JS 抽取接口契约——path、baseURL、签名盐、密文 id 公钥、hidden 路由、写死演示号，并据此构造不依赖会话的请求
template: none
---
## 前端契约抽取

目标站的真实接口面通常只在前端 bundle 里。抽的时候**不只抽 `/api/` path**——下面每一类
抽到就进清单，抽不到写"无"，不要因为没抽到就整站停工。

| 抽到什么 | 用来干什么 |
|----------|------------|
| `/api/` 等接口 path | 进接口清单，标 METHOD |
| `baseURL` / `apiHost` / `env.js` / `/prod-api` | 找到真正的业务 host，可能不是当前页面那个域 |
| 签名盐、硬编码密钥 | 自己算签名，打"只要 sign、不要 Cookie"的口 |
| 密文 id + 前端公钥（`modulus`/`exponent`、JSEncrypt、`security.js`） | 自己加密邻号再换，见下 |
| 异步 chunk、sourcemap、路由里的 hidden/admin | 页面不展示也直接打对应 API |
| 写死的演示号、测试租户、体验入口 | 当钥匙用。这和对着登录框喷 `admin/admin` 不是一类 |

## 签名请求

有 `sign` / `_token` / `x-sign` / `timestamp+nonce` 这类参数时，先读签名函数再复现，不要猜。
常见三种形态：

- `md5(按 key 排序后的 k=v 拼接 + 盐)`
- `hmacSha256(timestamp + nonce + body, appSecret)`
- `md5(userId + ':' + timestamp + ':' + salt)`

复现后用一条对照验证：真签名过、假签名被拒，两者回包不同才算复现成功。
有些口只要签名不要会话——这类口是未授权测试的入口，优先打。

## 密文对象 id

详情/列表的 id 是一长串密文、而 JS 里有公钥时，对象引用并没有消失，只是被前端包了一层。
用页面公钥加密相邻整数（或页面上写死的演示 `userid`）再换进去，就是一次普通越权测试。
对照用：会话绑死只回自己、服务端拒乱加密串、或者换完仍是公开 FAQ —— 这三种算证伪，
不要当成"加密了所以测不了"。

## 降级路径（禁止空等）

| 场景 | 做法 |
|------|------|
| 纯 API / 几乎无业务 JS | Swagger/OpenAPI、抓包、响应里的 link / next page 字段、常见网关 path |
| JS 太重或拉不下来 | 抓包 + HTML 内联脚本 + 已知 path，不要等"抽干净"再开工 |
| 打开是登录页 | 先找业务 API 或跳转后的业务 host，主业挖那边的未登录面；见 `analysis/differential_oracle` 的登录闸收敛 |
| SPA 渲染不出来 | `agent-browser` 快照拿 DOM 与网络请求，再回到 curl |

有前端却清单空就去瞎跑目录扫描，和无 JS 就整站停工，是两个反向的浪费。

## 沙箱内怎么做

本沙箱**没有** js-reverse / 浏览器调试类 MCP。可用的只有：

- `exec_command` + `curl` 拉 bundle，`grep -oE` 提取 path、`baseURL`、密钥字面量；
- `agent-browser`（经 `exec_command` 调用）做页面快照、点击、取网络请求；
- `repeat_request` 基于已捕获请求改参重放。

不要把上面这些换成不存在的工具名。另外授权闸门只放行只读方法，且命令里的每个 host
都要在白名单内——抽到别的域的接口只是线索，不能直接打。
