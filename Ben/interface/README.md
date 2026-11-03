# Interface 模块布局

- `main.py`：统一程序入口，负责命令分发与启动初始化。
- `cli/`：命令行参数、认证、补全、环境检查、扫描准备及无交互执行。
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
