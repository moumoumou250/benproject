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
│   │   ├── request_log.py      # 请求日志
│   │   └── warmup.py           # 预热
│   ├── runtime/                # Docker 运行时（原 containers/ 中的连接逻辑 + sandbox/）
│   │   ├── docker_connection.py    # 稳定的 Docker 守护进程连接
│   │   ├── docker_client.py        # DockerSandboxClient（SDK sandbox 后端）
│   │   ├── docker_executor.py      # 容器生命周期 + 容器内执行 + 超时处理
│   │   ├── session_manager.py      # 沙箱会话管理
│   │   ├── deferred_session.py     # 延迟启动会话
│   │   ├── backends.py             # 后端选择
│   │   ├── caido.py                # Caido 代理集成
│   │   ├── status.py               # 运行状态
│   │   └── check_docker_connection.py  # 连接自检脚本
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
│   └── skills/                 # 知识片段目录（scan_modes / analysis / coordination /
│                               #   reconnaissance / technologies / vulnerabilities /
│                               #   protocols / tooling / custom）
├── containers/                 # Docker 镜像构建资源
│   ├── Dockerfile
│   └── docker-entrypoint.sh
├── scripts/
│   └── smoke_core.py           # 核心模块导入 + 对象构造冒烟（不调用模型，0 token）
└── reports/                    # report_*.md / trace_*.json 输出目录
```

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
| `ALLOWED_TARGETS` | 授权目标白名单，逗号分隔；默认 `http://juice-shop:3000` |
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
通过容器名 `http://juice-shop:3000` 互访；默认不挂载宿主机目录、不挂 Docker
socket、不使用特权容器。

## 七、按顺序验证（建议逐步进行）

```powershell
# [1] Docker 执行器：启动/复用、退出码、stdout/stderr、超时与恢复（无需 Key）
.\.venv\Scripts\python.exe main.py --selftest-docker

# [2] 工具层：真实在容器内对授权目标执行一次 HTTP 检查（无需 Key）
.\.venv\Scripts\python.exe main.py --selftest-tools

# [3] 模型文本调用：确认 Key / 接口 / 模型可用（需要 .env）
.\.venv\Scripts\python.exe main.py --selftest-llm
```

## 八、运行完整任务

```powershell
# 对授权目标跑完整 Agent 循环（默认目标取白名单首项）
.\.venv\Scripts\python.exe main.py http://juice-shop:3000/

# 等价方式：直接运行包内入口
.\.venv\Scripts\python.exe -m Ben.interface.main http://juice-shop:3000/

# 任务结束保留执行容器（默认自动删除）
.\.venv\Scripts\python.exe main.py http://juice-shop:3000/ --keep-container

# 输出 DEBUG 级详细日志
.\.venv\Scripts\python.exe main.py -v
```

## 九、输出说明

任务结束后在 `reports/` 生成两个文件（时间戳命名）：

- `report_YYYYMMDD_HHMMSS.md`：最终测试报告，含目标、实际操作、执行结果、
  证据、结论（已验证/疑似/未发现）、后续建议；
- `trace_YYYYMMDD_HHMMSS.json`：结构化执行轨迹，含每轮工具调用参数、真实结果、
  结束原因，便于调试与复核。

每次新报告落盘时，此前的历史 report/trace 自动按月份移入
`reports/archive/YYYYMM/`，根部只保留最新一次产出。

## 十、安全约束

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
  启动官方 filesystem 服务器；list/search/schema/call 四步派发实测通过
  （`_mcp_live_probe.py`）。
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
