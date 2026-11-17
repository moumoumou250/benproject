# 代码执行层对照：sapper-v2 vs Ben

> 范围：只讨论**命令/代码怎么被执行**（执行载体、超时、输出、状态、失败恢复），不重复编排与提示词议题。
> 方法：逐行读 sapper-v2 与 Ben 的实现，并用 15 个历史 run、392 次工具调用、173 次 `exec_command` 做实测。
> 结论中的"预期效果"均为估算，标注了推算依据；验证方式见 §7。

---

## 0. 结论摘要

**sapper 的代码执行不是"一个工具"，而是"把执行权整体外包给一个长驻 CLI 会话，编排层只在进程边界上加围栏"。**
Ben 相反：执行被切碎成"一次工具调用 = 一条命令 = 一次 LLM 往返"，并且把 **等待窗口（yield）当成了命令超时（kill）**。

| 维度 | sapper-v2 | Ben 现状 |
|---|---|---|
| 执行载体 | `claude -p` 子进程，自带 Bash 工具，交互式会话 | 进程内 SDK `Shell` capability → Docker `exec_run` |
| 执行单元 | **1 个 intent = 1 个进程**，进程内可跑任意多条命令 | **1 条命令 = 1 次工具调用 = 1 次 LLM 往返** |
| 命令时长 | 不限制单条；只在 dispatch 级 600s 和挑战级 10min 收口 | `yield_time_ms` 兼作硬超时，默认 **10s**、上限 **30s** |
| 超时后 | 杀掉整个 CLI 进程，图保留，可 `--continue` 续跑 | `pkill -f '<完整命令行>'`，长命令/heredoc 常匹配失败 |
| 会话状态 | 交互式 shell，cwd/env/后台作业/反弹 shell 天然保留 | 每次 `docker exec` 独立，**无状态**；PTY 能力存在但 0 使用 |
| 结果契约 | 只回一个 JSON：title + 一句 description + `doc` 落盘 | 原始 stdout 全量进历史（再做头尾截断） |
| 长任务 | 直接跑，跑到完 | 必须 `&` + log + 轮询读回（61% 的命令这么干） |
| 失败恢复 | watchdog `--continue` 重启、key 轮换、dispatch 重试 3 次 | auto_resume/nudge、模型重试退避、轮次上限 |

一句话：**sapper 用"进程"隔离复杂度，Ben 用"工具调用"暴露复杂度。** 沙箱隔离两边都够，差的是执行单元和结果契约。

---

## 1. sapper 是怎么执行代码的

### 1.1 执行权外包：编排层不实现任何命令工具

`lib/sapper.py:1427 run_claude()` 是唯一出口：

```
claude -p --output-format json --model <m> --dangerously-skip-permissions [--append-system-prompt ...]
```

- `cwd` = 挑战目录（`worker.sh spawn` 里 `cd $dir && ... auto`，`docker/commander/worker.sh:410`）
- 输入 = 渲染后的 prompt（`stdin`），输出 = 一个 JSON envelope（`lib/sapper.py:1490-1502`）
- 命令怎么跑、跑多久、输出多大、怎么截断，**全部由 CLI agent 自己的 Bash 工具决定**，sapper 不介入

因此 sapper 没有 shell schema、没有命令白名单、没有输出截断逻辑。它只有三样东西：**进程、cwd、prompt**。

### 1.2 三层围栏：只围"进程"，不围"命令"

| 层 | 实现 | 默认值 | 作用点 |
|---|---|---|---|
| dispatch 级 | `subprocess.run(..., timeout=timeout)` `lib/sapper.py:1490-1501` | `SAPPER_TIMEOUT=600` (`:4492`) | 单次 `claude -p` 的总时长 |
| run 级 | `deadline = deadline_from_args(args)`；每步 `time.time() >= deadline` 即停 (`:3758-3763`) | `--minutes 10`（`worker.sh:47 DEF_MINUTES=10`） | 一个挑战的工作预算 |
| step 级 | `max_steps` / `max_explore` (`:3743-3748, :4613`) | 10 / 3 | reason→explore 的迭代次数 |

注意：这三层**都不限制单条命令**。`nmap -p-` 可以跑 9 分钟，只要在 `--minutes` 内；到点由进程级 timeout 整体杀掉，图（fact graph）已经落盘，所以损失可控。

### 1.3 上下文与结果契约：执行过程不进父上下文

一个 intent 一个进程，因此：

- 进程内的几十条命令、几万行输出，只存在于这个进程的会话里；
- 进程结束时，父层只拿到 `_parse_explore_payload()` 解出的**一个 JSON**（`lib/sapper.py:3891-3914`）;
- 长证据必须写进 `doc`，而 `doc` **不注入 prompt**：`render_graph_text()` 只输出 `title + description[:70~90] + 📄标记`（`:1153-1182`），要用时再 `sapper doc <id>` 读。

`templates/explore.md` 把这条规则写死：

> `description`（REQUIRED）— a ONE-sentence gist ... `doc`（OPTIONAL）— detailed markdown: the exact commands you ran and their output ... Put the long evidence here, NOT in description.

这是 sapper 最值得抄的一点：**执行痕迹落在磁盘，不落在对话里**。

### 1.4 状态与恢复：进程会死，状态不会

- 交互式 pane（`worker.sh spawn` 不带 `-p`）里，cwd/env/后台任务/反弹 shell 都是**同一个 shell 会话**，不需要每条命令 `cd`；
- watchdog 检测 pane 退出后 `--continue` 重启（`docker/commander/watchdog.sh:289, :380-389`），恢复的是**CLI 会话**；
- 配额错误时先轮换 key 再重试（`lib/sapper.py:1459-1487`），重试 3 次（`SAPPER_RETRY_MODEL=3`）；
- commander 读 worker 输出只 `peek <pane> 40`（`worker.sh:514-521`，默认 40 行），board 只给计数不给图。

### 1.5 sapper 没做好的地方（不能照抄）

1. **无单命令超时**：一条 `nc` 挂住可以吃掉整个 dispatch，连带 9 分钟的挑战预算。
2. **无命令级遥测**：执行了什么、多久、多大，全在 pane 滚屏里，run 结束即丢。
3. **无输出脱敏/限流**：CLI 的 Bash 输出直接进 CLI 上下文，靠 CLI 自己的 30k 字符截断。
4. **认领无过期**：dispatch 被 timeout 杀掉后 intent 仍是 `claimed`（`lib/sapper.py:522-535` 无 TTL），同一 worker id 能自愈，换 id 就卡住。

所以正确做法不是"改成 sapper"，而是**保留 Ben 的进程内可控性，补上 sapper 的执行单元与结果契约**。

---

## 2. Ben 现在是怎么执行代码的

### 2.1 装配链

```
factory._make_shell_tools (factory.py:1181)
  └─ Shell(configure_tools=_configure_shell_tools).bind(session).tools()
       ├─ exec_command  → _wrap_exec_command (factory.py:834)
       └─ write_stdin   → _wrap_write_stdin  (factory.py:885)
```

`_wrap_exec_command` 做四件事：profile shell allow → 授权白名单 → 默认 `shell=bash` → `max_output_tokens` 夹紧（`_apply_shell_output_cap`, `:741`）。

### 2.2 一次 exec 的真实路径：yield 就是 kill

```
_wrap_exec_command
  └─ tty 为假 → _run_one_shot_shell (factory.py:789)
       └─ ExecCommandArgs.model_validate(parsed)
       └─ timeout_s = args.yield_time_ms / 1000        # ← 关键
       └─ _shell_tool._run_one_shot_exec(timeout_s=timeout_s, ...)
            └─ session.exec(command, timeout=timeout_s)
                 └─ DockerSandboxSession._exec_run(kill_on_timeout=True)  # docker.py:587-631
```

三个直接后果：

1. **默认只能等 10 秒**。SDK 的 `_DEFAULT_EXEC_YIELD_TIME_MS = 10_000`；`PTY_YIELD_TIME_MS_MAX = 30_000` 决定了模型再怎么填也不会等超过 30 秒。
2. **超时不是"先返回、稍后读"，是"杀掉"**。`asyncio.wait_for` 到点后进 `kill_on_timeout` 分支，命令被终止，返回 `Command timed out after N seconds.`。
3. **清理靠 `pkill -f '<完整命令行>'`**（`docker.py:517-530`）。对 4KB 的 heredoc、含 `[`/换行的命令，pattern 基本匹配不上，**该杀的子进程会留在容器里继续跑**。

而工具描述（`factory.py:753-759`）写的是：

> "A non-TTY command still running at the yield cannot be read later (write_stdin fails on it and its output is lost)"

这句话只说了"读不到"，没说"已经被杀了"。模型据此会以为进程还在后台跑，于是改用 `&` + 读日志——这正是下面 61% 后台率的来源。

### 2.3 输出与历史

- 一次性 `exec_run(demux=True)` 会把 stdout/stderr **全量缓冲在宿主内存**，结束后才返回；超大输出先占内存，再被 `output_store` 截断。
- 截断策略是 `head 1000 行 + tail 1000 行 + 提示 + spill 路径`（`Ben/tools/output_store.py:81-133`），默认上限 2000 行 / 50KB。
- 截断后的文本**原样进历史**，跟着之后每一轮一起重发。

### 2.4 没有作业抽象

全仓 grep 只有提示词教模型 `cmd > /tmp/out.log 2>&1 &`（`factory.py:756`），没有 job id、没有完成检测、没有 exit code 回收、没有 kill 接口。

---

## 3. 实盘数据（15 runs / 392 tool calls / 173 exec）

| 指标 | 数值 | 含义 |
|---|---:|---|
| `exec_command` 占全部工具调用 | 173 / 392 = **44%** | 执行是主路径 |
| `tty=true` | **0 / 173** | 持久会话能力完全闲置 |
| `write_stdin` 调用 | **0** | 同上 |
| `workdir` 参数 | **0 / 173** | 全靠命令里自己 `cd` |
| 显式 `max_output_tokens` | 2 / 173 | 输出上限基本由默认值决定 |
| `yield_time_ms` 分布 | 10s×38、25s×36、15s×30、30s×22、默认×22、20s×11、28s×6、8s×3 | 模型在手动试不同等待时间 |
| **超时次数** | **21 / 173 = 12.1%** | 每 8 条命令就有 1 条被杀 |
| 含 `&` 的命令 | **106 / 173 = 61.3%** | 后台化是主要绕行手段 |
| 读日志（`tail`/`cat`） | 47 / 173 = 27.2% | 后台化的代价：额外往返 |
| heredoc 内联脚本 | 49 / 173 = 28.3% | 平均命令 934 字符，最长 4,374 |
| 显式 `timeout` 包裹 | 16 / 173 | 模型自己补超时 |

单条输出 token：**p50 = 97，p90 = 1,838，max = 10,650**。

三个可以直接下结论的事实：

1. **12% 的命令被静默杀死**，且模型不知道"被杀"还是"没输出"；
2. **61% 的命令在手工模拟作业系统**，其中 27% 还要额外一轮去读日志——这是纯粹的往返税；
3. **持久 shell 能力（PTY/write_stdin）一次都没被用**，说明默认路径把能力挡住了。

---

## 4. 差距表（只列会改变行为的）

| # | 差距 | sapper | Ben | 影响 |
|---|---|---|---|---|
| D1 | 单命令时长 | 不限（进程级 600s 兜底） | 硬顶 30s，默认 10s | 扫描类命令跑不完 |
| D2 | 超时语义 | 杀进程、图保留、可续跑 | 杀命令、`pkill` 可能漏杀、语义未告知模型 | 重复劳动 + 孤儿进程 |
| D3 | 状态 | 会话级 | 每条命令独立壳 | 环境/工作目录反复重建 |
| D4 | 长任务 | 直接跑 | `&` + 轮询日志 | 27% 额外往返 |
| D5 | 结果契约 | 一句摘要 + doc 落盘 | 原始输出进历史 | 上下文膨胀 |
| D6 | 恢复 | watchdog `--continue` | auto_resume（仅对话层） | 执行中断后从头来 |
| D7 | 遥测 | 无（pane 滚屏） | 无 | 都无法调参 |

---

## 5. 优化方案

排序依据：**先修"命令跑不完"，再修"跑了看不见"，最后才修省钱**。这不是一个省钱优化，主收益是执行力与墙钟；token 是顺带。

### P0-1 解耦 `yield_time_ms` 与命令超时

**问题**：`_run_one_shot_shell` 把 `yield_time_ms/1000` 直接当 `session.exec(timeout=...)`。

**改法**：

1. 在 `_configure_shell_tools` 里给 `exec_command` 的 `params_json_schema` 补两个字段：
   ```json
   "timeout_s": {"type": "integer", "minimum": 1, "maximum": 600,
                 "description": "Hard limit for this command. Default 60. Use background mode for longer work."},
   "mode": {"type": "string", "enum": ["wait", "background", "auto"], "default": "auto"}
   ```
2. `_run_one_shot_shell` 改为：
   ```python
   timeout_s = float(parsed.get("timeout_s") or settings.exec_default_timeout_s)  # 默认 60
   timeout_s = max(1.0, min(timeout_s, settings.exec_max_timeout_s))             # 上限 600
   ```
   `yield_time_ms` 在 one-shot 路径**不再参与超时**，仅用于 background 模式的"先等多久再返回"。
3. 新增配置：`BEN_EXEC_TIMEOUT_S=60`、`BEN_EXEC_MAX_TIMEOUT_S=600`（放 `ContextSettings` 或新的 `ExecSettings`）。

**代价**：schema 多约 90 tok/轮；相对 27k 固定前缀可忽略。

**预期**：12.1% 的超时率降到 <3%（依据：现有 21 次超时里，`yield=25~30s` 的占 13 次，给到 60s 大概率能跑完）。

### P0-2 容器内硬超时，别再靠 `pkill -f`

**问题**：宿主侧 `wait_for` 到点后，靠 `pkill -f '完整命令行'` 清理，长命令/heredoc 匹配失败。

**改法**：在 `_run_one_shot_shell` 里把命令包一层 coreutils `timeout`：

```python
wrapped = f"timeout -k 5 {int(timeout_s)} {shell} -lc {shlex.quote(command)}"
output, exit_code, _ = await _shell_tool._run_one_shot_exec(
    session=session, command=wrapped,
    timeout_s=timeout_s + 8,          # 宿主多等 8s，让容器内的 timeout 先收口
    ...
)
```

**收益**：
- `timeout` 默认给子进程建独立进程组并整组发信号，**孙进程一起死**，不留孤儿；
- 返回 `exit=124` 是明确信号，模型能区分"超时"与"失败"；
- 不再依赖 pattern 匹配，4KB heredoc 也能杀干净。

**注意**：镜像需有 coreutils（当前 `kali-rolling` 自带）；Windows 后端若走 `docker exec` 同样成立。

### P0-3 作业句柄：把 61% 的自制后台变成一等公民

**问题**：模型用 `&` + log + `tail` 手工模拟作业系统，没有 pid、没有 exit code、没有完成通知。

**改法**：新增一个工具 `job`（单 schema，`action` 枚举，约 180 tok），配一个 `Ben/runtime/jobs.py`：

```python
# 启动（mode=background）
jid = uuid4().hex[:8]
launch = (
    f"mkdir -p /workspace/.jobs && "
    f"setsid bash -lc {shlex.quote(cmd + '; echo $? > /workspace/.jobs/%s.exit' % jid)} "
    f"> /workspace/.jobs/{jid}.log 2>&1 < /dev/null & "
    f"echo $! > /workspace/.jobs/{jid}.pid; echo {jid}"
)
```

- `job(action="list")` → 每个 job：`jid / state(running|done|killed) / exit / elapsed / bytes / cmd[:60]`
- `job(action="output", id=..., tail=80)` → 状态 + 末尾 80 行 + 总行数/字节 + 完整日志路径
- `job(action="kill", id=...)` → `kill -TERM -<pgid>`，2s 后 `-KILL`
- `mode="auto"`：命令首个 token 命中 `nmap|masscan|nuclei|ffuf|gobuster|dirb|wfuzz|sqlmap|hydra|nikto|whatweb` 时自动转入后台，立即返回 jid

**额外收口**：run 结束时在 `runner` 的 `finally` 里 `job kill --all`，避免容器里残留扫描进程。

**预期**：27% 的"读日志"往返改成一次结构化查询；`sleep 30; cat log` 从两轮变一轮。按 173 次 exec 里 47 次读日志估算，可省 **20~30% 的执行相关往返**。

### P1-1 执行结果信封化 + 摘要进历史

**问题**：原始 stdout 全量进历史，p90 1,838 tok、max 10,650 tok。

**改法**：在 `_run_one_shot_shell` 返回前包一层信封（不改 SDK）：

```
[exec] exit=124 timeout=true duration=60.1s bytes=182345 lines=4211 truncated=true spill=/workspace/.tool-output/ab12.txt job=-
--- head (first 60 lines) ---
...
--- tail (last 20 lines) ---
...
```

- exec 专用上限：`BEN_EXEC_OUTPUT_LINES=400`、`BEN_EXEC_OUTPUT_BYTES=12KB`（区别于通用工具的 2000/50KB）
- 超限即 spill 全文，历史里只留摘要 + 路径 + 一句"用 `grep`/`read` 取细节"
- 与 P0-3 联动：background 启动只返回 jid + 首 20 行，不再把整段日志塞回来

**预期**：单条执行输出 p90 从 1,838 降到 ~700 tok；173 条命令累计约省 15~18 万 tok（跨 15 个 run，约 1.2 万/run）。属于中等收益，但它同时降低"晚期每轮重发"的斜率。

### P1-2 持久 shell 会话（opt-in）

**问题**：`tty` 与 `write_stdin` 全仓 0 使用，cwd/env 每条命令重建。

**改法**：

1. `BEN_EXEC_SESSION=oneshot|pty`，默认 `oneshot`（保持现状，避免 Windows+Docker Desktop 已知的 PTY 丢输出问题）；
2. 首次启用时做能力探测：PTY 下跑 `echo __BEN_PTY_OK__`，5s 内拿到回显才启用，否则回退并缓存结论；
3. 会话命令用哨兵收口：
   ```
   {cmd}; printf '\n__BEN_DONE_%s__\n' $?
   ```
   读 `write_stdin` 直到哨兵，剥 ANSI 与提示符，保留 exit code。

**收益**：`cd`/`export`/虚拟环境/后台作业自然保留；对需要多步交互的目标（ssh、msf、gdb）是质变。
**风险**：PTY 噪声、命令交错、Windows 后端不稳定 —— 所以做成 opt-in + 探测。

### P1-3 批量命令（sapper"一个进程多条命令"的轻量版）

**改法**：`exec_command` 增加可选 `cmds: [str]`（与 `cmd` 二选一），顺序执行、逐条打点：

```
for i, c in enumerate(cmds):
    print(f"\n=== [{i}] $ {c[:120]}")
    run(c); print(f"[exit={code}]")
```

**约束**：遇非零退出即停（除非 `keep_going=true`）；总输出仍走 P1-1 信封。
**预期**：49 条 heredoc 里的探测循环、`for p in ...; do curl; done` 可合并，减少 1~2 轮/次。

### P2-1 命令级遥测

写到 `<run>/.state/exec.jsonl`：`{ts, cmd_sha, cmd_head, exit, duration_ms, stdout_bytes, timed_out, job_id, spill}`。
用途：验证 P0 的效果、按 profile 调超时、发现"总在超时的命令族"。当前这一层完全缺失。

### P2-2 per-intent 时间盒

给 coverage/finding 的领取项加 `deadline`（类似 sapper `--minutes`）；到点注入一条系统提示"时间盒到，返回部分结果"，而不是让 agent 无限试。
**注意**：这属于编排层，和 `scan_clock` 的 run 级 deadline 是两个粒度，不要混用。

### P2-3 收口清理

run 结束 / 子 agent 结束时统一 `job kill --all` + `pkill -f /workspace/.jobs`，防止容器里遗留扫描进程影响后续判定。

---

## 6. 最小可行补丁路线

不建议一次做完。按"两周内能验证"的粒度：

**第 1 步（约 120 行）**：P0-1 + P0-2
- 改 `factory.py:_run_one_shot_shell`、`_wrap_exec_command`、`_configure_shell_tools`
- 加 `BEN_EXEC_TIMEOUT_S` / `BEN_EXEC_MAX_TIMEOUT_S`
- 不动 schema 也能先验证：先把 `timeout_s` 从 `yield_time_ms` 解耦为固定 60s，观察超时率

**第 2 步（约 150 行）**：P0-3
- 新 `Ben/runtime/jobs.py` + `Ben/tools/jobs.py`
- 挂进 `_BASE_TOOLS` 的 exec 侧（仅拥有 shell 的 profile）
- runner `finally` 清理

**第 3 步（约 80 行）**：P1-1
- `_run_one_shot_shell` 返回信封；exec 专用截断常量

**第 4 步（可选）**：P1-2 持久会话，先做探测与开关，不改默认路径。

回归判定沿用项目基线：相关单文件 pytest 通过即可（全量有约 99 个预存在的环境相关失败，见 `.workbuddy/memory/MEMORY.md`）。改动 `exec_command` 描述会触发提示词/工具 schema 断言测试，需同步更新。

---

## 7. 怎么验证（别拿感觉验收）

构造三类基准命令，同一目标、同一 profile，改造前后各跑 3 次：

| 用例 | 命令 | 期望（改造后） |
|---|---|---|
| A 短命令 | `curl -sS -m 5 http://target/` | 不变，<1s |
| B 中时长 | `nmap -sT -p- --min-rate 2000 target` | 默认 60s 内不超时；超时则 exit=124 且无孤儿 |
| C 长任务 | `nuclei -u target -severity high` | `mode=auto` 自动转后台，返回 jid；`job output` 一次拿到进度与 exit |

验收指标（对比本次基线）：

1. `timed_out / exec_calls` 从 **12.1% → <3%**
2. 含 `&` 的命令从 **61% → <35%**（作业工具接管）
3. 读日志往返从 **27% → <10%**
4. `exec` 输出 p90 token 从 **1,838 → <800**
5. 容器内 `ps` 在 run 结束后无遗留 `nmap/nuclei/sqlmap`

---

## 8. 哪些 sapper 的做法不采纳

| 做法 | 为什么不抄 |
|---|---|
| tmux pane 作为执行单元 | Ben 是单进程 asyncio + AgentCoordinator，引入 pane 会把生命周期、日志、权限全部打散；P1-2 的持久 shell 已能拿到 80% 收益 |
| `--dangerously-skip-permissions` 的 CLI 委派 | Ben 的授权白名单、profile shell allow、网络策略都在进程内，外包给 CLI 等于放弃这层控制 |
| 完全不做单命令超时 | 这正是 sapper 的缺陷（一条 `nc` 吃掉整个 dispatch），Ben 不能学 |
| `doc` 只落盘、不注入 | 方向对，但 Ben 需要保留"关键证据进历史"的能力；采用"摘要 + 指针"而非"只给 id" |

---

## 9. 证据索引

**sapper-v2**
- `lib/sapper.py:1427-1502` `run_claude` / `_run_engine_once`（进程、timeout、重试、key 轮换）
- `lib/sapper.py:3727-3870` `cmd_auto`（deadline、step/explore 上限、no-progress 停机）
- `lib/sapper.py:3891-3914` `_run_explore_raw`（一 intent 一进程、JSON 契约）
- `lib/sapper.py:1118-1182` `render_history_block` / `render_graph_text`（description 截 70~90 字、doc 只给标记）
- `lib/sapper.py:4492` `SAPPER_TIMEOUT` 默认 600
- `docker/commander/worker.sh:47, :410, :514-521`（10 分钟/挑战、`cd $dir` 长驻、peek 40 行）
- `docker/commander/watchdog.sh:289, :380-389`（`--continue` 重启）
- `docker/Dockerfile:88-95`（非 root agent 用户）
- `templates/explore.md`（三层文档契约、timebox 纪律）

**Ben**
- `Ben/agents/factory.py:741-748` `_apply_shell_output_cap`
- `Ben/agents/factory.py:751-766` `_EXEC_YIELD_CAP_S` / 工具描述
- `Ben/agents/factory.py:789-831` `_run_one_shot_shell`（`timeout_s = yield_time_ms/1000`）
- `Ben/agents/factory.py:834-882` `_wrap_exec_command`
- `Ben/agents/factory.py:908-931` `_configure_shell_tools`
- `Ben/agents/factory.py:1181-1202` `_make_shell_tools`
- `Ben/config/settings.py:311-340` 输出上限配置
- `Ben/tools/output_store.py:81-133` 头尾截断
- `.venv/.../agents/sandbox/capabilities/tools/shell_tool.py:24, :90-102, :105-133`（默认 10s、一次性执行、参数 schema）
- `.venv/.../agents/sandbox/session/pty_types.py:11` `PTY_YIELD_TIME_MS_MAX = 30_000`
- `.venv/.../agents/sandbox/sandboxes/docker.py:495-533, :587-631`（kill_on_timeout、`pkill -f`）
