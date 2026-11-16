# BenProject — 最小漏洞测试 Agent（OpenAI 兼容大模型 + Docker）

一个**可实际运行**的最小漏洞测试项目：将漏洞挖掘工作流拆分为独立的
「预设 Prompt、模型调用、工具执行、Docker 容器、结果处理」模块。
当前默认接**智谱 GLM（glm-5.3-flash，免费）**，客户端基于 OpenAI 兼容接口，
改两行配置即可切换 DeepSeek。已内置轻量 Skill 机制（`Ben/skills/` 知识片段目录，
渲染时按角色/模式内联共享片段，运行时可经 `load_skill` 工具按需加载），但仍不
是完整 Agent 框架。支持**黑盒 / 白盒双模式**（白盒经 `is_whitebox` 渲染开关与
`scan_modes/whitebox.md` 片段启用：源码仅用于定位疑点，结论仍以受控工具真实
响应为准）。定位为**授权安全研究 / 披露 / 教育**用途：授权范围内积极推进、
不误拒；范围外目标一律不碰。

## 一、工作流程

```
用户输入授权目标
  -> 加载预设 Prompt（Ben/agents/prompts/system_prompt.jinja）
  -> 组装任务上下文
  -> 调用 DeepSeek（OpenAI 兼容接口）
  -> 解析响应，判断是否提出工具调用（function call）
  -> 校验工具名称与参数（JSON Schema + 授权白名单）
  -> 在 Docker 容器内真实执行（非特权、无挂载、专用网络）
  -> 把真实 stdout/stderr/退出码返回模型，进入下一轮
  -> 模型不再调用工具 / 达到轮数上限
  -> 生成并保存测试报告（含执行轨迹）
```

**决策与执行分离**：模型提出工具调用只代表意图；只有执行器返回真实结果，才认定
操作发生。模型无法让任何命令绕过校验直接执行。

## 二、目录结构

包结构参照 strix（`strix/` 包 → `Ben/` 包），源码集中在 `Ben/`，Docker 资源与
运行产物留在项目根。

```
BenProject/
├── main.py                     # 根入口：转发到 Ben.interface.main（等价 -m Ben.interface.main）
├── requirements.txt            # 直接依赖
├── .env.example               # 配置模板（无真实密钥）
├── Ben/                        # Python 包（对应 strix/）
│   ├── agents/                 # Agent 构建与预设 Prompt
│   │   ├── factory.py          # 基于 openai-agents SDK 的 SandboxAgent 构建
│   │   ├── prompt.py           # 预设 Prompt 加载、Jinja 渲染与 skill 片段合并
│   │   └── prompts/
│   │       ├── system_prompt.jinja   # 分七节的主模板（含 available_skills 目录）
│   │       └── scope.jinja           # 授权范围片段
│   ├── config/
│   │   ├── settings.py         # 读取 .env / 环境变量并校验（pydantic-settings）
│   │   ├── loader.py           # 配置合并（env > .env > override JSON > 默认值）
│   │   ├── models.py           # BenProvider / 模型包装（流式守卫、turn guard）
│   │   ├── codex.py            # Codex 账号认证
│   │   ├── tool_call_arguments.py  # 畸形 tool-call 参数修复
│   │   ├── tool_call_ids.py        # 空/重复 tool-call id 修复
│   │   └── tool_call_limits.py     # 并行工具调用数限制
│   ├── core/
│   │   ├── runner.py           # 多轮循环、工具反馈、报告/轨迹落盘
│   │   ├── execution.py        # 单轮执行与重试
│   │   ├── agents.py           # Agent 生命周期与完成判定
│   │   ├── hooks.py            # SDK 钩子（预算、空闲超时等）
│   │   ├── inputs.py           # 输入归一化
│   │   ├── paths.py            # 运行目录布局
│   │   └── sessions.py         # 会话持久化
│   ├── interface/
│   │   ├── main.py             # 入口：正常任务 + 三种自检
│   │   ├── cli_args.py         # 命令行参数解析
│   │   └── targets.py          # 授权目标解析
│   ├── llm/                    # 上下文与请求侧辅助（不再有 deepseek_client）
│   │   ├── compaction.py       # 历史压缩
│   │   ├── context_budget.py   # 上下文预算
│   │   └── request_log.py      # 请求日志
│   ├── runtime/                # Docker 运行时（原 containers/ 中的连接逻辑 + sandbox/）
│   │   ├── docker_connection.py    # 稳定的 Docker 守护进程连接
│   │   ├── docker_client.py        # DockerSandboxClient（SDK sandbox 后端）
│   │   ├── docker_executor.py      # 容器生命周期 + 容器内执行 + 超时处理
│   │   ├── session_manager.py      # 沙箱会话管理
│   │   ├── deferred_session.py     # 延迟启动会话
│   │   ├── backends.py             # 后端选择
│   │   ├── caido.py                # Caido 代理集成
│   │   └── status.py               # 运行状态
│   ├── tools/                  # 工具族（按子包划分，无单一 registry.py）
│   │   ├── thinking/           # think
│   │   ├── todo/               # create_todo / list_todos / update_todo / mark_todo_done
│   │   ├── notes/              # create_note / update_note / list_notes
│   │   ├── coverage/           # record_coverage
│   │   ├── reporting/          # list_reports
│   │   ├── threat_model/       # 威胁建模
│   │   ├── agents_graph/       # 子 Agent 编排
│   │   ├── proxy/              # 代理控制
│   │   ├── web_search/         # 联网检索
│   │   ├── agent_browser/      # 浏览器自动化
│   │   ├── mcp/                # MCP 客户端（stdio / http）
│   │   ├── wait_for_user/      # 等待用户
│   │   ├── finish/             # agent_finish / finish_scan
│   │   ├── load_skill/         # 只读知识片段加载（按名取 Ben/skills/ 正文）
│   │   ├── output_store.py     # 长输出落盘与截断
│   │   └── nullish.py          # nullish 值归一化
│   │   (shell 工具由 SDK Shell capability 生成：exec_command/write_stdin)
│   ├── report/                 # 报告与用量
│   │   ├── writer.py           # Markdown 报告
│   │   ├── sarif.py            # SARIF 输出
│   │   ├── coverage.py         # 覆盖率对账
│   │   ├── dedupe.py           # 漏洞去重
│   │   ├── pricing.py          # LiteLLM 价格解析
│   │   ├── usage.py            # 用量账本
│   │   ├── runs.py             # 运行记录
│   │   └── state.py            # 运行状态快照
│   ├── rag/                    # 知识资料向量化与 RAG 检索（LangChain 不编排）
│   │   ├── knowledge_base/     # 知识资料
│   │   ├── embeddings/         # 向量化
│   │   ├── vector_store/       # 向量知识库
│   │   ├── retriever/          # 检索
│   │   └── tools/              # 检索工具入口
│   └── skills/                 # 操作规范与执行流程（scan_modes / analysis / coordination /
│                               #   reconnaissance / technologies / vulnerabilities /
│                               #   protocols / tooling / custom）
├── containers/                 # Docker 镜像构建资源
│   ├── Dockerfile
│   └── docker-entrypoint.sh
├── scripts/
│   └── smoke_core.py           # 核心模块导入 + 对象构造冒烟（不调用模型，0 token）
└── reports/                    # report_*.md / trace_*.json 输出目录
```

操作规范放在 `Ben/skills/`；知识资料通过 `Ben/rag/` 写入向量知识库并按需检索。
LangChain 仅用于 RAG，不参与 Agent 编排。RAG 已接入资料入库、SQLite 向量持久化、命令行检索和 Agent 检索工具，详见 [RAG 说明](Ben/rag/README.md)。

## 三、环境要求

- Windows / macOS / Linux，已安装 **Docker Desktop / Docker Engine** 并启动；
- Python **3.12**（项目自带 `.venv`，也可新建）；
- 一个 **OpenAI 兼容 API Key**：默认用智谱（https://bigmodel.cn/ 申请，`id.secret`
  形式）；也可用 DeepSeek（https://platform.deepseek.com/ 申请）。

## 四、安装

```powershell
# 进入项目
cd C:\Users\19219\WorkBuddy\BenProject

# （可选）新建虚拟环境；项目已含 .venv 可跳过
python -m venv .venv

# 安装依赖
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 五、配置

```powershell
# 复制模板为 .env，再填入真实 Key
Copy-Item .env.example .env
notepad .env
```

`.env` 关键项（模板中无真实密钥）：

| 变量 | 说明 |
|---|---|
| `LLM_API_KEY` | 通用 API Key；带前缀模型会自动镜像到厂商变量（如 `MOONSHOT_API_KEY`） |
| `LLM_MODEL` | 模型名。裸名走 `LLM_API_BASE` 端点；`厂商/模型` 走 LiteLLM 目录路由 |
| `LLM_API_BASE` | OpenAI 兼容端点地址（模式 A 必填，模式 B 留空） |
| `ALLOWED_TARGETS` | 授权目标白名单，逗号分隔，必填。只从项目根 `.env` 读取；未配置时拒绝启动，没有内置默认目标。示例：`http://target.example:8080` |
| `MAX_AGENT_TURNS` | 最大决策轮数，防止无限循环，默认 12 |
| `COMMAND_TIMEOUT` | 单条容器命令超时，默认 30 秒 |

**两种接入模式**（多厂商架构移植自 strix，路由经 LiteLLM）：

- **模式 A · OpenAI 兼容端点**：裸模型名 + `LLM_API_BASE` + `LLM_API_KEY`，
  适合智谱/KIMI/DeepSeek 官方兼容入口。例：`LLM_MODEL=kimi-k2.6`，
  `LLM_API_BASE=https://api.moonshot.cn/v1`。
- **模式 B · LiteLLM 多厂商路由**：模型名带前缀（如 `moonshot/kimi-k2.6`、
  `deepseek/deepseek-chat`、`openrouter/z-ai/glm-5.3`），走 LiteLLM 内置目录
  （100+ 厂商），`LLM_API_BASE` 留空。key 用通用 `LLM_API_KEY` 或厂商专属
  变量（如 `MOONSHOT_API_KEY`，此时通用 key 可留空）。
  注意：部分厂商目录默认国际站端点，cn 区 key 需用厂商 `*_API_BASE` 覆盖
  （如 `MOONSHOT_API_BASE=https://api.moonshot.cn/v1`）。

历史别名 `DEEPSEEK_MODEL` / `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` 仍兼容，
优先级低于 `LLM_*`。去重判重模型可用 `DEDUPE_LLM_API_KEY` / `DEDUPE_LLM_API_BASE`
指向与主模型不同的端点。

## 六、启动授权靶场（OWASP Juice Shop）

```powershell
# 创建专用网络（执行器自检也会自动创建）
docker network create benptnet 2>$null

# 启动 Juice Shop 并加入专用网络；映射 3000 便于浏览器查看
docker run -d --name juice-shop --network benptnet -p 3000:3000 `
  --restart unless-stopped bkimminich/juice-shop:latest
```

浏览器打开 http://localhost:3000 确认靶场就绪。执行容器与靶场在同一专用网络，
通过容器名互访。若本次授权目标就是这个容器，把 `.env` 的 `ALLOWED_TARGETS`
写成 `http://juice-shop:3000`。这只是本地靶场示例，程序不会在未配置时自动使用该地址。
默认不挂载宿主机目录、不挂 Docker socket、不使用特权容器。

## 七、按顺序验证（建议逐步进行）

```powershell
# [1] Docker 执行器：启动/复用、退出码、stdout/stderr、超时与恢复（无需 Key）
.\.venv\Scripts\python.exe main.py --selftest-docker

# [2] 工具层：真实在容器内对授权目标执行一次 HTTP 检查（无需 Key）
.\.venv\Scripts\python.exe main.py --selftest-tools

# [3] 模型文本调用：确认 Key / 接口 / 模型可用（需要 .env）；
#     模型分层时同时验证总节点 LLM_MODEL 与子节点 BEN_FLASH_MODEL，
#     子节点未配置则跳过（子 agent 共用总节点），任一已配置节点不可用则退出 1
.\.venv\Scripts\python.exe main.py --selftest-llm
```

## 八、运行完整任务

```powershell
# [0] 目标预检（只读，不建 run、不调用模型）：先确认目标真的可达
.\.venv\Scripts\python.exe main.py preflight http://target.example:8080/
.\.venv\Scripts\python.exe main.py preflight --target-list .\targets.txt --json

# 命令行 URL 必须落在 .env 的 ALLOWED_TARGETS 内。
# 不传 URL 时使用该白名单的第一项；白名单为空则拒绝启动。
.\.venv\Scripts\python.exe main.py http://target.example:8080/

# 等价方式：直接运行包内入口
.\.venv\Scripts\python.exe -m Ben.interface.main http://target.example:8080/

# 任务结束保留执行容器（默认自动删除）
.\.venv\Scripts\python.exe main.py http://target.example:8080/ --keep-container

# 输出 DEBUG 级详细日志
.\.venv\Scripts\python.exe main.py -v
```

预检做什么（三层，缺一不可）：

1. **authorize**：目标主机必须在 `ALLOWED_TARGETS` 内。不在的话工具层会逐条
   拒绝命令，扫描必然空转；
2. **probe-host**：宿主机视角 DNS → TCP → HTTP（不走环境代理，不跟随跳转）；
   只连上 TCP 不算可达，拿不到 HTTP 响应即判失败（透明代理/TUN 下任意 IP 都能
   "连上"，只有 HTTP 应答能证明站点真的活着）；
3. **probe-container**：在 `benptnet` 容器内 `curl`。宿主可达 ≠ 容器可达，
   `172.19.0.2` 这类地址只有容器视角的结论才算数。

**"通不通"和"能不能扫"分开报**：白名单外但站点活着，结论是「可达 · 未授权」，
不会被一句"不可达"掩盖掉——这两件事的补救动作完全不同（开服务 vs 改白名单）。

扫描启动时会自动跑一次同样的预检，不可达就**在创建 run 之前退出**，不消耗模型
额度；确认要跳过用 `--skip-preflight`，只想看宿主机视角用 `preflight --no-docker`。

`preflight` 的退出码：

| 码 | 含义 | 补救 |
|---|---|---|
| 0 | 可达且已授权 | 可以直接扫 |
| 1 | 不可达（DNS/端口/路由） | 先让服务起来 |
| 2 | 可达但未授权 | 加进 `ALLOWED_TARGETS` |

### 只做信息收集：`ben recon`

预检回答"通不通、授没授权"，`recon` 回答"**这是什么**"。它是确定性侦察：不调
模型、不建 run、不消耗额度，只发 GET，产物可 diff、可进 CI。

```powershell
# 默认：根路径 + 24 条定点路径 + TLS + 同源 JS 提取 → 产物落 recon/<host>_<时间戳>/
.\.venv\Scripts\python.exe main.py recon http://target.example:8080/

# 只要 JSON（管道用），不落盘
.\.venv\Scripts\python.exe main.py recon https://staging.example.com --json --no-write

# 不启动容器（只看本机网络位置）；或自定义路径清单
.\.venv\Scripts\python.exe main.py recon http://10.0.0.5:8080 --no-docker
.\.venv\Scripts\python.exe main.py recon http://host:3000 --path-list my-paths.txt --max-paths 40
```

产物（与 sapper 的事实图同构：`kind` + `data`，便于脚本或后续 run 直接读）：

```
recon/<host>_<时间戳>/
├── recon.json   结构化事实：host / service / http / tls / surface / endpoint / param / note
├── recon.md     人读版：清单 + 判定 + 建议下一步
└── raw/         证据原文（只留文本类响应，单文件截断）
```

**边界是刻意的**：

- 只发 GET；不提交表单、不登录、不爆破；
- 每条命令都过 `Ben/agents/factory.py:_check_command_allowed`——与 Agent 同一道
  闸门，CLI 不比 Agent 多一分权限；跨主机的 JS/静态引用只记录、不抓取；
- 目标必须在 `ALLOWED_TARGETS` 内，否则**退出 2、一个请求都不发**；
- 安全响应头缺失只进"建议"，按项目口径不认定为漏洞。

退出码：0 完成；1 不可达/执行失败；2 未授权。

### 判断该派谁去：`ben scout`

`recon` 回答"这是什么"，`scout` 回答"**下一步派谁、值不值得派**"。它是 sapper
里那个"4 分钟侦察兵"的确定性版本：**时间盒 + 端口/版本/入口面 + easy|medium|hard**，
不调模型、结果可复现，产物可以直接写成 sapper 的 fact。

```powershell
# 默认：4 分钟时间盒 + 常见端口 + 入口面优先的路径表
.\.venv\Scripts\python.exe main.py scout http://target.example:8080/

# 直接吐 sapper 命令，粘过去就上板
.\.venv\Scripts\python.exe main.py scout http://target.example:8080/ --emit sapper | sh

# 只扫指定端口；或完全不扫端口（只看 HTTP 面）
.\.venv\Scripts\python.exe main.py scout http://10.0.0.5:8080 --ports 22,80,443,8080
.\.venv\Scripts\python.exe main.py scout http://10.0.0.5:8080 --no-ports --no-docker
```

产物：

```
recon/<host>_<时间戳>_scout/
├── scout.json       事实 + 裁定（verdict：level/confidence/分数/每条依据）
├── scout.md         人读版：服务表 + 版本台账 + 入口面 + 裁定依据
├── facts.jsonl      每行一条 fact（喂给别的工具）
├── facts.sapper.sh  逐条 `sapper fact -k … -d …`（可直接 sh 执行）
└── raw/             证据原文
```

它比 `recon` 多做的三件事，以及各自的诚实边界：

| 新增 | 怎么做 | 边界 |
|---|---|---|
| **端口/服务** | 容器内 `nmap -sT`（有则用 `-sV` 分批对已开端口做版本识别）；无 nmap 退 `nc -z` | 镜像里的 nmap 带 file capabilities，可能 exec 失败（exit 126）：此时自动拷一份无特权副本跑 `-sT`；`nc` 只能回答"通/不通"，区分不了 closed 与 filtered；两者都不可用才跳过并说明，不降级到宿主机裸 socket（那会绕过授权闸门） |
| **版本台账** | 合并 nmap `-sV`、`Server`/`X-Powered-By`、正文指纹，以及已知应用的公开版本接口（如 Juice Shop 的 `/rest/admin/application-version`），按产品去重取最高置信度 | **钉不死是合法结论**：没有 high/medium 版本时输出 `payload_policy=no-blind-payload`，绝不猜一个版本出来；**只有 web 面版本算钉死**，ssh/mysql 的版本记作"旁路"不参与判定 |
| **难度裁定** | 确定性规则打分（easy/hard 两个方向，每条带权重与理由） | 是**排序判断**，不是漏洞结论，不进报告；置信 low 时建议"再侦察一次"而不是硬派 |

裁定 → 派单：`easy`/`medium` → 士兵（medium 加 15 分钟复查）；`hard` → 将军；全站
软 404 导致测量不可信时 → `scout-again`。软 404 与真实命中可以并存：只要有一条路径
与随机基线不同，就只信那几条，不整片否定路径结论。

边界与 `recon` 一致：只读（GET/HEAD + TCP 连接探测）、每条命令过同一道闸门、
目标不在 `ALLOWED_TARGETS` 内则退出 2 且零网络动作。退出码：0 完成；1 不可达/执行
失败；2 未授权。

### 聚焦单类漏洞：`ben vuln`

整轮扫描很贵：多个子 agent、全类型铺开、侦察与测试混在一起。当你只想确认
「这个站有没有 SQL 注入」时，用 `vuln` 只加载那一类漏洞的方法论，把范围收窄。

```powershell
# 先看有哪些类型（不需要 .env，不启容器，不花额度）
.\.venv\Scripts\python.exe main.py vuln --list

# 只测 SQL 注入：别名 sqli → Ben/skills/vulnerabilities/sql-injection.md
.\.venv\Scripts\python.exe main.py vuln sqli -t http://target.example:8080/ --max-turns 8

# 多个类型 / 加挂工具技能 / 先看会注入什么
.\.venv\Scripts\python.exe main.py vuln "xss,csrf" --skills tooling/nuclei -t http://target.example:8080/
.\.venv\Scripts\python.exe main.py vuln idor -t http://target.example:8080/ --dry-run

# 指定漏洞检出回归：必须在报告里命中子串，否则 exit 3
.\.venv\Scripts\python.exe main.py vuln idor -t http://target.example:8080/ `
  --expect IDOR --expect /api/users --max-turns 10 --max-budget 0.5
```

它做这些事，其余复用普通扫描链路（`prepare_run` → `AgentRunner` → `run_cli`），
因此授权闸门、预检、沙箱、报告与 run 记录完全一致：

1. 把 `sqli` / `SQL_Injection` / `lfi` 这类写法解析成 `vulnerabilities/<name>`；
2. **启动前**校验技能真的存在（`_resolve_skills` 对未知名是静默跳过，不校验的话
   你会得到一次"看起来在测 SQLi、其实没加载任何 SQLi 方法论"的扫描）；
3. 在 instruction 前拼一段 `<focused_scan>`：技能挂在子 agent、不做无关枚举、测完即
   `finish_scan`；
4. **代码强制** `BEN_MAX_SUBAGENTS=1`（不只靠提示词）；
5. 可选 `--expect`：对照 `vulnerabilities.json` 做检出判定，并打印调试摘要。

边界：技能聚焦仍主要靠提示词；子 agent 数量由环境变量硬闸。只读闸门不受影响——
需要请求体的探测走 `repeat_request`，`curl -d` / `sqlmap --data` 会被拒绝。

退出码：0 完成（且 expect 全命中）；2 类型无法解析/参数非法，或 `--fail-on` 命中；
3 `--expect` 未全部命中。

## 九、输出说明

任务结束后在 `reports/` 生成两个文件（时间戳命名）：

- `report_YYYYMMDD_HHMMSS.md`：最终测试报告，含目标、实际操作、执行结果、
  证据、结论（已验证/疑似/未发现）、后续建议；
- `trace_YYYYMMDD_HHMMSS.json`：结构化执行轨迹，含每轮工具调用参数、真实结果、
  结束原因，便于调试与复核。

每次新报告落盘时，此前的历史 report/trace 自动按月份移入
`reports/archive/YYYYMM/`，根部只保留最新一次产出。

## 十、安全约束

- 授权目标只来自项目根 `.env` 的 `ALLOWED_TARGETS`。未配置时明确报错并拒绝启动，
  不使用任何内置默认地址；`cli-config.json` 与进程环境变量不能覆盖该值；
- 仅测试授权白名单内目标；目标 origin 在启动时校验一次，exec_command 每条命令
  再逐主机校验一次（urlsplit 解析，userinfo/整数 IP 变形不生效；含 shell 变量或
  命令替换的网络命令一律拒绝，防止绕过）；
- 只读约束为**代码级硬闸门**：curl/wget 等网络客户端仅允许 GET/HEAD/OPTIONS，
  携带请求体或上传的标志（-d/--data/-F/-T/--json/--post-*）一律拒绝执行；
- MCP filesystem 仅暴露只读工具（裁剪 write_file/edit_file/create_directory/
  move_file），Agent 无法经 MCP 改写 `.env` 授权闸门；
- 白名单含公网地址时启动即打告警日志，提示确认书面授权；
- 执行容器非特权、无宿主机挂载、无 Docker socket，限制内存/CPU，使用专用网络；
- 缺少安全响应头仅作加固建议，**不认定为漏洞**；
- 证据不足时标记「未验证」，宁可漏报、不误报。

## 十一、当前验证状态

- **已实际验证（无需 Key）**：Docker 执行器（启动/复用、stdout/stderr、退出码、
  超时与恢复）、工具层（注册/Schema/白名单/参数校验/对靶场真实执行）、入口链路、
  全部文件语法编译。
- **已实际验证（含 Key）**：`--selftest-llm` 文本调用、function calling、
  完整 Agent 多轮循环、对公网授权靶标的端到端报告（见 `reports/`）。
- **已实际验证（MCP）**：stdio 连接经 `~/.ben/mcp-servers.json` 直连 node.exe
  启动官方 filesystem 服务器；list/search/schema/call 四步派发实测通过。
- 注：本项目在 Windows 上运行时，项目目录不得带 Low 完整性级别标签
  （`icacls . | findstr Mandatory` 应无输出），否则 Python 进程被降级，
  无法访问 Docker 命名管道。

## 十二、配套执行规则

项目内 `补充执行规则/` 为永久生效的研究执行规则目录（入口
[补充执行规则/AGENTS.md](补充执行规则/AGENTS.md)），覆盖：授权研究语境、
黑盒+白盒主流程、SRC 价值矩阵、报告格式、反过度说教、任务目录、迭代落盘等。
本项目 Agent 的 prompt 内嵌了其中与本程序相关的纪律（授权范围、证据分级、
报告要求）；人机协作层面的完整流程以该目录 rules 原文为准，冲突时按目录内
优先级裁决。


## ChatGPT / Codex subscription

Sign in using ChatGPT OAuth. Third-party subscription access is not an officially supported OpenAI integration.

```powershell
.\.venv\Scripts\python.exe main.py auth login chatgpt
.\.venv\Scripts\python.exe main.py auth status
# If the local callback is unavailable, paste the callback URL manually:
.\.venv\Scripts\python.exe main.py auth login chatgpt --manual
```

Set `LLM_MODEL=chatgpt/<model available to your account>` in `.env`.
This route does not require `LLM_API_KEY` or `LLM_API_BASE`.
Login does not change your model selection. Credentials are stored separately in
`~/.ben/subscription-auth.json`; expiring access tokens are refreshed automatically.

```powershell
# Make a real minimal request (consumes account usage):
.\.venv\Scripts\python.exe main.py --selftest-llm
# Remove the stored subscription credentials:
.\.venv\Scripts\python.exe main.py auth logout
```


### 跨平台开发检查

Windows、Linux 和 macOS 均可使用 `python scripts/dev.py <任务>`，支持
`test`、`lint`、`format-check`、`smoke`、`tui-build`、`tui-test`、`tui-lint`、`viewer`、`package`。
TUI 构建需要 Go 1.24+；Viewer 需要 Node.js；package 需要安装 Python build 包。

推荐使用 `uv sync --frozen` 安装锁定的开发环境，使用 `uv lock` 明确更新依赖，
提交 pyproject.toml、requirements.txt 和 uv.lock。requirements.txt 保留作为 pip 安装入口，
其直接依赖通过测试与 pyproject.toml 保持一致；需要完整锁定环境时使用 uv.lock。
GitHub Actions 检查 Python（Windows/Linux）、Viewer 类型与构建、Go TUI 和 wheel 安装。


### React 网站测试控制台

首次构建前端：

```powershell
cd Ben/interface/viewer/frontend
npm ci
npm run typecheck
npm run build
cd ../../../..
```

从项目根目录启动：

```powershell
.venv/Scripts/python.exe -m Ben.interface.web
```

浏览器会自动打开本地 React 控制台（默认 8765 端口）。也可使用终端打印的完整链接打开。
输入已授权的网站 URL，选择测试模式、轮数、预算，填写测试要求后启动。
页面显示执行日志，可进入实时进度、漏洞与报告视图；历史记录保存在 `strix_runs/`。
使用现有模型和 Docker 配置，无需另填 API key。`--no-open` 禁止自动打开浏览器，
`--port 端口号` 指定端口。关闭控制台不会终止已启动的扫描。

也可以在 React 前端目录执行 `npm start`，由前端启动入口自动启动 Python 服务并打开网页。
首次运行或修改前端后先执行 `npm run build`。
