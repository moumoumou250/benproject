# Interface 模块布局

- `main.py`：统一程序入口，负责命令分发与启动初始化。
- `cli/`：命令行参数、认证、补全、环境检查、扫描准备及无交互执行。
  - `target_check.py`：目标预检（`main.py preflight`），授权/宿主机/容器三层探测，
    扫描启动前默认执行一次，不可达就不建 run。
  - `recon.py`：只读信息收集（`main.py recon`），不调模型、不建 run；产物落
    `recon/<host>_<时间戳>/`。每条命令都过 `agents/factory.py:_check_command_allowed`
    与 Agent 同一道闸门，未授权直接退 2、零网络动作。
  - `scout.py`：侦察兵（`main.py scout`），在 recon 之上加端口/服务、版本台账与
    easy|medium|hard 裁定；产物落 `recon/<host>_<时间戳>_scout/`，`--emit sapper`
    可直接生成 `sapper fact` 命令。闸门与只读约束同 recon。
- `terminal/`：终端交互、恢复任务选择、统计展示和文本处理。
- `shared/`：各界面共享的目标解析与 URL 安全校验。
- `tui/`：Go TUI 源码、Python 桥接和协议。
- `viewer/`：本地报告 HTTP 服务、React 前端、扫描启动服务与静态资源。
- `web/`：Web 控制台启动入口。

启动方式保持不变：

```powershell
python main.py --help
python -m Ben.interface.main --help
python -m Ben.interface.web
```

模块引用使用完整功能路径，例如 `Ben.interface.cli.arguments`、
`Ben.interface.terminal.text` 和 `Ben.interface.shared.targets`。
