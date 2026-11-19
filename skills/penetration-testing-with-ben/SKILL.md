---
name: penetration-testing-with-ben
description: Pentest a web app, API, codebase, repository, URL, domain, or IP with Ben — autonomous AI penetration testing that exploits and proves vulnerabilities (OWASP Top 10 and beyond — injection, XSS, SSRF, auth/access-control flaws, IDOR, business logic) instead of just flagging them. Runs entirely self-hosted (Docker sandbox + your own LLM key) and returns validated findings with proof-of-concept exploits (Markdown, JSON, CSV, SARIF). Use when the user asks to pentest, hack, security-scan, security-audit, or find vulnerabilities in an app, API, website, or repo.
license: Apache-2.0
metadata:
  author: useben
  homepage: https://docs.ben.ai
---

> **入口说明（Ben 移植版）**：文中 `ben` 指本项目命令行入口 `python main.py`（等价 `python -m Ben.interface.main`）。
> 托管云（原 Strix Cloud / app.strix.ai）相关流程已剔除，本文只覆盖本地执行路径。

# Run a Ben pentest

Ben runs autonomous AI pentesting agents that dynamically exploit a target and only report findings validated with a working proof-of-concept. In this project there is **one execution path**: the local CLI, which runs the agents in a Docker sandbox with your own LLM key.

> 本移植不含托管云（原 Strix Cloud）：不需要账号、不需要网络连接器、不需要上传源码，全部在本地容器内完成。

## When this fits

| Situation | Notes |
|---|---|
| Docker 可用、有 LLM Key | ✅ 本项目唯一支持的路径 |
| 源码不能出本地（隐私/隔离） | ✅ 天然满足：容器不挂载宿主机目录 |
| 免费 / 一次性 / 本地开发回路 | ✅ 只消耗你自己的模型费用 |
| 没有 Docker 或没有 LLM Key | ❌ 本项目不提供托管替代方案，需先准备 Docker 与模型 |

---

# 运行本地 CLI

## Prerequisites

1. **Docker running** — check with `docker info`. The first scan pulls the sandbox image automatically.
2. **Ben 就绪** — 在项目根目录检查入口可启动：
   ```bash
   python main.py --selftest-llm     # 模型连通性自检（需要 .env 里的 Key）
   python main.py --selftest-docker  # Docker 执行器自检（不需要 Key）
   ```
   首次安装依赖：`pip install -r requirements.txt`。
3. **LLM configured** — 写在项目根的 `.env`（或环境变量）里：
   ```bash
   LLM_MODEL=openrouter/z-ai/glm-5.3     # 裸模型名走 LLM_API_BASE；带前缀走 LiteLLM 目录
   LLM_API_KEY=<provider api key>
   ```
   Ask the user for these if unset. Never hardcode or commit keys.

## Running a scan

Always use `-n` (non-interactive/headless) — the default TUI blocks agents. Always set `--max-budget` unless the user says otherwise.

```bash
# Local code (white-box)
python main.py -n -t ./ --scan-mode standard --max-budget 10

# Deployed app / API (black-box)
python main.py -n -t https://staging.example.com --max-budget 20

# Repo + deployed app together (best coverage)
python main.py -n -t https://github.com/org/app -t https://staging.example.com

# Focused testing with credentials or scope hints
python main.py -n -t https://app.example.com \
  --instruction "Use credentials user@example.com:pass123. Focus on IDOR and auth bypass."

# API spec as a first-class target (OpenAPI/Swagger or a Postman collection export)
python main.py -n -t ./openapi.yaml -t https://api.staging.example.com

# Many targets from a file, one per line
python main.py -n --target-list ./targets.txt --max-budget 30

# Give the agents a file to work with (wordlist, spec, notes) without making it a target
python main.py -n -t https://staging.example.com --workspace-file ./wordlist.txt --max-budget 20
```

A local path passed with `-t` is mounted into the sandbox **writable** — the agents can read and modify it, so point at a clean checkout, not uncommitted work you care about.

Key flags:

| Flag | Meaning |
|---|---|
| `-t, --target` | URL, repo URL, local path, domain, IP, OpenAPI/Postman spec, or `postman://<uuid>`. Repeatable. |
| `--target-list PATH` | File of targets, one per line (`#` comments allowed). Repeatable, combines with `-t`. |
| `-n, --non-interactive` | Headless, exits on completion. Required for agents. |
| `-m, --scan-mode` | `quick` (minutes) / `standard` (~30 min) / `deep` (hours, default). |
| `--instruction` / `--instruction-file` | Credentials, focus areas, scope rules. |
| `--workspace-file PATH[:DEST]` | Copy a file from this machine into `/workspace` before the scan, for a wordlist, a spec, or notes. Repeatable. |
| `--max-budget USD` | Hard LLM spend cap; scan wraps up cleanly at the limit. |
| `--max-turns N` | Per-agent turn cap (default 500). |
| `--fail-on SEVERITY` | Headless only: exit `2` only for findings at or above `critical`/`high`/`medium`/`low`/`info`. Default: any finding. |
| `-r`, `--resume RUN_NAME` | Resume a prior run from `_artifacts/runs/`, with its agent history and targets. Cannot be combined with `-t`. |
| `--scope-mode` | For code targets: `auto` (diff-scope in CI/headless), `diff` (force changed files only), `full` (whole tree). |
| `--diff-base REF` | Branch or commit that `diff` scope compares against. Defaults to the repo's default branch. |

Scans take minutes (`quick`) to hours (`deep`). Run them in the background and poll for completion rather than blocking.

### Exit codes (headless)

- `0` — finished with no validated vulnerabilities **in what was analyzed** (with `--fail-on`, none at or above the threshold; lower ones are still in the artifacts)
- `1` — fatal error (missing env vars, Docker down, bad config)
- `2` — vulnerabilities found (with `--fail-on`, at least one at or above that severity)

A `0` is not proof of full coverage: if `--max-budget`/`--max-turns` is reached before the scan completes, it wraps up early and still exits `0`. When you need assurance the scan finished, give it enough budget and check `_artifacts/runs/<run>/run.json`: a hard budget stop leaves `status: "stopped"`, but an agent that wrapped up early on a budget *warning* still calls `finish_scan` and records `"completed"` — so also sanity-check the run's cost against `--max-budget` and the report's stated coverage before treating a clean result as full coverage.

### Reading results

Artifacts land in `_artifacts/runs/<run-name>/`:

| File | Contents |
|---|---|
| `penetration_test_report.md` | Executive report — read this first. |
| `vulnerabilities/*.md` | One file per validated finding, with PoC and remediation. |
| `vulnerabilities.json` / `vulnerabilities.csv` | All findings as structured JSON / CSV index. |
| `findings.sarif` | SARIF 2.1.0 for GitHub code scanning / ASPM ingestion. |
| `run.json` | Run metadata, status, targets, usage/cost. |

---

---

## Reporting & next steps

Summarize findings by severity (critical/high/medium/low/info) and include the PoC evidence. To remediate and verify fixes, use the **fix-security-vulnerabilities-with-ben** skill. To wire scanning into CI/CD, use the **ci-security-scanning-with-ben** skill.

## Safety

Only scan targets the user owns or is authorized to test. 确认授权由你自己负责；Ben 另有代码级硬闸门（.env 里的 `ALLOWED_TARGETS` 白名单 + 只读命令校验），目标不在白名单内会直接拒绝执行。
