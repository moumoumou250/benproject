# BenProject — 最小漏洞测试 Agent（OpenAI 兼容大模型 + Docker）

一个**可实际运行**的最小项目：把参考项目（clown-src-6k-skill）的漏洞挖掘工作流，
转化为独立的「预设 Prompt、模型调用、工具执行、Docker 容器、结果处理」模块。
当前默认接**智谱 GLM（glm-5.3-flash，免费）**，客户端基于 OpenAI 兼容接口，
改两行配置即可切换 DeepSeek。当前阶段不包含 Skill 加载/发现机制，也不是完整
Agent 框架。

## 一、工作流程

```
用户输入授权目标
  -> 加载预设 Prompt（Ben/agents/prompts/vulnerability_testing.txt）
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
│   │   ├── factory.py          # 基于 openai-agents SDK 的 SandboxAgent 构建（实验路径）
│   │   ├── prompt.py           # 预设 Prompt 加载
│   │   └── prompts/
│   │       └── vulnerability_testing.txt   # 分七节的固定测试指导
│   ├── config/
│   │   └── settings.py         # 读取 .env / 环境变量并校验（原根 config.py）
│   ├── core/
│   │   └── runner.py           # 多轮循环、工具反馈、报告/轨迹落盘
│   ├── interface/
│   │   ├── main.py             # 入口：正常任务 + 三种自检
│   │   └── cli_args.py         # 命令行参数解析
│   ├── llm/
│   │   └── deepseek_client.py  # DeepSeek 封装 + 错误处理 + function calling
│   ├── runtime/                # Docker 运行时（原 containers/ 中的连接逻辑 + sandbox/）
│   │   ├── docker_connection.py    # 稳定的 Docker 守护进程连接
│   │   ├── docker_executor.py      # 容器生命周期 + 容器内执行 + 超时处理
│   │   └── check_docker_connection.py  # 连接自检脚本
│   └── tools/
│       ├── registry.py         # 工具注册、Schema 校验、执行分发
│       └── http_check.py       # 只读 HTTP 连通性 + 响应头检查
├── containers/                 # Docker 镜像构建资源
│   ├── Dockerfile
│   └── docker-entrypoint.sh
├── skills/                     # 技能目录（预留）
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
| `DEEPSEEK_API_KEY` | 真实 API Key（变量名为历史命名，智谱/DeepSeek 通用），仅存本地 `.env` |
| `DEEPSEEK_MODEL` | 默认 `glm-5.3-flash`（智谱免费，function calling 经实测稳定） |
| `DEEPSEEK_BASE_URL` | 默认 `https://open.bigmodel.cn/api/paas/v4`（智谱） |
| `ALLOWED_TARGETS` | 授权目标白名单，逗号分隔；默认 `http://juice-shop:3000` |
| `MAX_AGENT_TURNS` | 最大决策轮数，防止无限循环，默认 12 |
| `COMMAND_TIMEOUT` | 单条容器命令超时，默认 30 秒 |

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

## 十、安全约束

- 仅测试授权白名单内目标；工具层对目标 origin 做二次校验，白名单外一律拒绝；
- 当前仅允许只读 GET/HEAD；禁止写入、删除、爆破、DoS、横向移动、登出操作；
- 执行容器非特权、无宿主机挂载、无 Docker socket，限制内存/CPU，使用专用网络；
- 缺少安全响应头仅作加固建议，**不认定为漏洞**；
- 证据不足时标记「未验证」，宁可漏报、不误报。

## 十一、当前验证状态

- **已实际验证（无需 Key）**：Docker 执行器（启动/复用、stdout/stderr、退出码、
  超时与恢复）、工具层（注册/Schema/白名单/参数校验/对靶场真实执行）、入口链路、
  全部文件语法编译。
- **待提供 Key 后验证**：`--selftest-llm` 文本调用、function calling 工具调用、
  完整 Agent 多轮循环、Juice Shop 端到端报告。代码已就绪，配置好 `.env` 后按
  第七、八节命令即可重复运行。
