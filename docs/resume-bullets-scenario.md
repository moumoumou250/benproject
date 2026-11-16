# 简历条目（业务场景版）

> 每条 = 具体业务场景 → 代码层具体调整 → 技术要点/可验证结果。数据均来自本机真实运行记录。

## 一、调度与长跑稳定性

1. 针对并行派单时多个子 Agent 重复测同一接口（如同时打 `/api/login`）的问题，在 spawn 前引入磁盘认领：对 `(surface, risk_area)` 归一化后抢占，键与 coverage 账本严格一致避免双账本冲突，spawn 抛错时在 `except BaseException` 中回滚释放，崩溃则由 1.5 倍任务时限 TTL 兜底解锁，使重复面从"事后去重"前移为"事前占位"。

2. 交互式会话用户中途离开时，看护协程曾把"等用户输入"误判为卡死并杀掉整个会话；改为判定 `wait_kind` 属于 `user`/`agents` 或处于预算暂停态即跳过，仅在心跳超时且无在飞 LLM 调用时触发优雅停止（先 after-turn 再升级 immediate cancel）。

3. 长跑中子 Agent 名额被早完成的 agent 占满、连替换死掉的都无法派生，导致整次扫描停摆；把并发闸从"累计总数"改为"同时在飞"语义，并单独以 `BEN_MAX_GENERALS=1` 限制旗舰档，使 scout/validator 不挤占昂贵配额。

4. 扫描后期 root 反复调 `list_coverage` / `view_agent_graph` 空转至轮次耗尽；改为每轮对 coverage 数、漏洞数、完成 todo、终态 agent 取内容签名（而非 mtime 或长度）比对，连续 3 轮不变即注入收尾指令，把"该不该停"从一次模型推理降为一次确定性元组比较。

5. 断电或容器被杀时 claim 丢失会导致两个 Agent 同时开工；把四处重复的"临时文件 + os.replace"收敛为统一 `atomic_write_json` 并补 fsync，同时将 run 级时限从 `time.monotonic()` 改为墙钟，使 `-r` 续跑时剩余时间不被静默重置为完整时钟。

## 二、成本与上下文

6. 真实 10 次靶场扫描累计 715 万输入 token，其中 599 万命中前缀缓存（83.7%）；靠 `<cache_point>` 把系统提示切成稳定前缀与可变后缀，只让随任务变化的 skill 片段落到缓存点之后，保证同配置 Agent 的前缀字节级一致。

7. 针对"tools 在 system 之前"的请求结构，断点只打在 system 与末条消息，靠首个 system 断点顺带缓存工具定义，不浪费 Claude 仅有的 4 个缓存位；Bedrock 未映射模型与裸 `claude-*` 名直接不注入，规避端点报错与 SDK 的 `TypeError`。

8. 84 个 Skill 全量注入会把子 Agent 首轮上下文撑到 9,339 token；将目录改为"按类只列 name"，正文交给 `load_skill` 按需加载（单次 ≤5 个 / ≤24,000 字符，超限显式回报 `limit_chars`），系统提示降至 6,813 token（−27%）。

9. 多个子 Agent 共享历史发现时全量注入易撑爆上下文、完全不注入又会重走死路；以 90 字符索引行、最多 30 条注入，`clip_line` 超长时回退至最近空格且断点须过半程（避免切断单词），并按 findings > methodology > general 加时间戳排序，确保被截掉的是旧的低价值项。

10. 侦察与深度利用同档跑 flash，深度任务被拖累、旗舰又浪费在窄任务上；引入 tier 四档角色，用 `_FACTORY_MODEL_SENTINEL` 区分"未传 `model_name`"与"显式传 `None`"，让 general 显式取旗舰而不被静默降级。

## 三、质量与基础设施

11. 配置缺陷、凭据泄露这类非 HTTP 发现没有 exchange-id 硬门可依；补上 fail-closed 验证闭环，派独立 validator 只拿 claim/evidence/falsifier 且不继承 hunter 上下文，`refuted` 条目在 `finish_scan` 收口时移出报告正文，HTTP 发现则按 `http_exchange_ids` 识别后跳过、不重复占用验证额度。

12. 离线靶场无外网，不能依赖托管向量库；用 SQLite 单表加精确余弦在本地完成 58 万字语料、629 个知识块的 Top-K 检索，并以 embedding 签名比对与维度校验拦截"换了模型仍用旧向量"的静默错误。

13. 容器内 Agent 需访问宿主机 Caido 代理且执行过程要可复现；构建专用 user-defined network 替代默认 bridge（后者跨容器可互访、隔离策略失效），经 `host.docker.internal` 打通宿主机，并移除 SDK 的 `entrypoint=["tail"]` 覆盖，确保 `docker-entrypoint.sh` 真正执行、caido-cli 在容器内启动。

14. 安全策略由 11 类 84 个 Skill 承载，改策略不该动代码；以 `register_skill_dir` 支持外部目录扩展，并用提示词回归测试锁定"至多两个决定""升级阶梯""root 指令不得出现在子 Agent"等行为契约，使 markdown 策略迭代与 Python 机制解耦。

---

## 数据出处（面试追问时可直接答）

| 数字 | 来源 |
|---|---|
| 48,406 行 / 156 个 .py | `find Ben -name "*.py" \| xargs wc -l` |
| 11 类 / 84 个 Skill | `find Ben/skills -name "*.md" \| wc -l` |
| 583,176 字符 / 629 chunks | `Ben/rag/knowledge_base` + `select count(*) from chunks` |
| 9,339 → 6,813（−27%） | `docs/prompt-slimming-audit.md`，tiktoken o200k 实测 |
| 715 万 / 599 万 / 83.7% | `strix_runs/*/run.json` 的 `llm_usage` 汇总（10 次运行） |
