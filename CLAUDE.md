# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

BenProject is an authorized-scope vulnerability testing agent: an OpenAI-compatible LLM drives tools that execute inside a Docker sandbox against a whitelisted target, with every action gated in code. The package is `Ben/`; it is a local-only port of strix (names and call sites deliberately mirror upstream strix, so when in doubt read the docstrings — many are Chinese and explain *why* a call site exists).

Two invariants govern almost every design decision here:

1. **Decision ≠ execution.** The model proposing a tool call is only intent. Nothing is treated as having happened until the executor returns a real observation. Code that would let the model's claims substitute for evidence is a bug.
2. **Authorization is enforced in code, not in the prompt.** The prompt tells the agent the rules; `Ben/agents/factory.py` refuses the call.

## Commands

```bash
# Tests / lint (Python 3.12; venv already present)
.venv/Scripts/python.exe -m pytest -q                 # Windows
.venv/bin/python -m pytest -q                         # POSIX
.venv/Scripts/python.exe -m pytest tests/test_todo.py -q
.venv/Scripts/python.exe -m pytest "tests/test_todo.py::test_coerce_never_raises" -q
.venv/Scripts/python.exe -m pytest tests/test_todo.py -k coerce -q
.venv/Scripts/python.exe -m pytest -q -x --lf         # stop at first failure / rerun failures

.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format .
.venv/Scripts/python.exe scripts/smoke_core.py        # import+construct smoke, 0 tokens

# Cross-platform wrapper for the above plus TUI/viewer/package tasks
python scripts/dev.py test|lint|format-check|smoke|viewer|tui-build|tui-test|tui-lint|package
```

`make install|test|lint|format|smoke|viewer|tui-build|docker|clean` wraps the same commands; on Windows the Makefile resolves `PY` to `.venv/Scripts/python.exe`.

### Running the product

```bash
# Self-tests, in order of increasing cost
python main.py --selftest-docker     # container lifecycle, exit codes, timeouts — no API key
python main.py --selftest-tools      # real HTTP probe from inside the container — no API key
python main.py --selftest-llm        # real model call — needs .env

# A scan
python main.py http://juice-shop:3000/          # interactive Go TUI when a terminal is attached
python main.py -n -t <url> -m quick --max-turns 20 --max-budget 1 --instruction "..."
python main.py -r <run_name>                    # resume a prior run from strix_runs/<name>/run.json
python main.py --keep-container -v

# React scan console (see "Interfaces")
python -m Ben.interface.web            # default port 8765, opens browser
python main.py view [<run>]            # read-only viewer for one run
```

`-n/--non-interactive` is what the web console passes; without it and with a TTY attached, `main()` hands off to the Go TUI and falls back to headless on TUI failure (`Ben/interface/main.py:399-421`).

The sandbox image is `settings.executor_image` (`EXECUTOR_IMAGE` in `.env`, `Ben/config/settings.py:381`, consumed at `Ben/core/runner.py:758`); the default tracks an upstream `ghcr.io/usestrix/strix-sandbox` tag. Beware the two other image settings — `BEN_IMAGE` (`:285`, default `...:1.3.1`) and `DockerExecutor`'s own fallback (`Ben/runtime/docker_executor.py:85`, `...:1.3.0`) — they are for other code paths and differ in tag. `make docker` / `scripts/docker.sh` builds a local `ben-sandbox:dev` from `containers/Dockerfile` instead.

## Architecture

### Model routing

`Ben/config/models.py` wraps the openai-agents SDK model and swaps in a LiteLLM-backed provider for any non-OpenAI prefix (`BenProvider._resolve_prefixed_model`, `Ben/config/models.py:612`) — a bare model name plus `LLM_API_BASE` goes straight to that endpoint, while `moonshot/...`, `openrouter/...`, `chatgpt/...` are rewritten to LiteLLM routes. The same module enforces streaming guards, turn limits, and repairs malformed tool-call payloads (`Ben/config/tool_call_arguments.py`, `tool_call_ids.py`, `tool_call_limits.py`) — provider-compatibility shims, not general utilities; keep them narrow.

Opt-in RAG: `Ben/rag/` (SQLite vector store, local or OpenAI embeddings) exposes `search_knowledge` into `_BASE_TOOLS` (`Ben/agents/factory.py:62`). LangChain is used only here for splitting/embeddings, never for orchestration.

### Control flow of one scan

`Ben/interface/main.py:main()` → `_run_task()` → `Ben/interface/cli/scan_setup.py:prepare_run()` → `Ben/core/runner.py:AgentRunner`:

1. `Settings` from `.env` (`Ben/config/settings.py`, pydantic-settings).
2. `AgentCoordinator` — the multi-agent graph: statuses, mailbox, `agents.json` snapshots (`Ben/core/agents.py:56`).
3. `Ben/runtime/session_manager.py:create_or_reuse()` — Docker sandbox session, optionally deferred while Caido sidecar starts.
4. `Ben/agents/factory.py:build_agent()` — root agent, plus a child factory that backs `create_agent`.
5. `Ben/core/execution.py:run_agent_loop()` → `_run_cycle()` — the actual SDK Runner loop, retries, transient-error recovery, budget parking.
6. Teardown → `AgentRunner._persist()` writes `reports/report_<stamp>.md` + `reports/trace_<stamp>.json`, archiving older ones into `reports/archive/YYYYMM/` (`Ben/core/runner.py:1433`).

The run record is also written incrementally: `strix_runs/<run_name>/run.json` (status, usage, targets), `.state/agents.json` + `.state/agents.db` (per-agent session history), plus `vulnerabilities.json`, `vulnerabilities.csv`, `findings.sarif`, `coverage.json`, `vulnerabilities/<id>.md`, and `penetration_test_report.md`. Both `Ben/core/paths.py` and the web viewer read that directory — it is the single source of truth for a run, live or finished. Note the two report surfaces are different files: the executive report inside the run dir (`penetration_test_report.md`) vs. the timestamped copies `_persist()` drops in `reports/` for the CLI.

### Multi-agent model and the root-as-orchestrator split

`create_agent` / `wait_for_agents` / `send_message_to_agent` / `stop_agent` / `view_agent_graph` live in `Ben/tools/agents_graph/tools.py`; spawning is `Ben/core/execution.py:spawn_child_agent()` (`:430`), which wraps each child in its own `asyncio` task (`:1297`) in the same process. `agent_finish` (children) or `finish_scan` (root) terminates a branch.

The root agent is a **pure orchestrator** (commit `dc2bc7c`): `system_prompt.jinja` wraps the root in `<root_agent_directive>` telling it to delegate recon/testing/validation to children and *not* run scanners or probes itself. Every "you do X" instruction elsewhere in the prompt means "delegate X" when the reader is root. When changing prompt text or tool guidance, check which `is_root` branch you are in — the same paragraph reads differently for root and child.

### Prompt and skills assembly

`Ben/agents/prompt.py` renders a Jinja system prompt from `Ben/agents/prompts/` (main template + scope fragment) merged with reusable fragments from `Ben/skills/`.

- Skill names are `category/name`; bare names resolve across categories and raise on ambiguity (`Ben/skills/__init__.py`).
- Assembly order is fixed and deterministic so agents with identical parameters share an identical prompt prefix: user-requested → `scan_modes/<mode>` (+ `scan_modes/diff`) → reporting/coverage fragments → root fragment → whitebox fragment (`Ben/agents/prompt.py:67`). Prefix stability matters: a hoisted `<cache_point>` marker splits the immutable prefix from the variable suffix for prompt caching.
- `scan_mode` is one of `quick|standard|deep` (CLI `-m`, default `deep`); it selects a `scan_modes/<mode>.md` fragment. Whitebox is a separate axis, inferred from the target kinds — any `local_code` target (`is_whitebox_scan`, `Ben/interface/shared/targets.py:492`) turns it on and selects the `scan_modes/whitebox` fragment. The string `blackbox` is a *fragment* name, and `AgentRunner.scan_mode` defaults to `"blackbox"` even though the CLI never produces that value (`Ben/core/runner.py:490`), so don't assume CLI `-m` and the fragment key share a namespace.
- `load_skill` (`Ben/tools/load_skill/tool.py`) lets a running agent pull fragment bodies on demand, max 5 per call.

Two skill directories exist and they are **not** interchangeable: `Ben/skills/` holds the fragments the renderer merges into system prompts, while `skills/` at the repo root holds the standalone Anthropic-style `SKILL.md` packages (unchanged from upstream) — code does not load those.

### Interfaces

- **Go TUI** (`Ben/interface/tui/`): real Go app, built to `Ben/bin/ben-tui.exe` (committed). Python side runs a JSON-over-socket backend server; protocol is versioned in `Ben/interface/tui/backend/protocol.py` (`PROTOCOL_VERSION`, capability list) and the Go side mirrors it in `internal/protocol`. Changing the wire format means bumping the version and touching both sides — `tests/test_tui_protocol_conformance.py` guards this. `Ben/interface/tui/sidecar.py` launches/supervises the binary and deliberately strips `*_API_KEY`/secret env vars before spawning it.
- **React web console** (`Ben/interface/viewer/`): stdlib `ThreadingHTTPServer` serving the prebuilt SPA from `Ben/interface/viewer/static/` (committed build output) plus a JSON API (`/api/runs`, `/api/run`, `/api/vulnerabilities`, `/api/report`, `/api/transcript`, `/api/report.pdf`, `POST /api/scans`). The page is authorized by a token in the operator link, which mints a session cookie (`Ben/interface/viewer/server.py`). `launcher.py` starts scans as a subprocess with `-n` and never through a shell. Source is `frontend/` (Vite + React 19 + Tailwind 4); after changing it run `npm run build` in `frontend/` and commit the new `static/` output — the server serves the committed bundle, not a dev server.
- **Headless CLI** (`Ben/interface/cli/runner.py`): rich Live view, degrades to plain text when stdout is not a TTY or `-v` is set.

### Security gates (do not weaken casually)

- Target whitelist `ALLOWED_TARGETS`: checked once at startup (`Ben/core/runner.py:295`) and again per command at tool invocation (`Ben/agents/factory.py:558 _check_command_allowed`, hostname extraction at `:458`). Shell-variable / command-substitution forms are rejected outright so the host cannot be resolved at execution time instead of check time.
- Read-only enforcement (`Ben/agents/factory.py:511 _check_command_readonly`): curl/wget/http/nc/socat may only use GET/HEAD/OPTIONS; payload/upload flags (`-d/--data/-F/-T/--json/--post-*`) are refused.
- MCP filesystem is configured read-only, with the write tools trimmed out (`~/.ben/mcp-servers.json`, `mcp_servers/filesystem/`), explicitly so an agent cannot rewrite `.env` and widen its own whitelist.
- The sandbox container is unprivileged, has no host mounts and no Docker socket, and runs on a dedicated network.
- `_warn_public_whitelist` (`Ben/core/runner.py:325`) logs when the whitelist contains public hosts.

Tests that encode these invariants: `tests/test_whitelist_gate.py`, `tests/test_agent_factory_shell.py`, `tests/test_agent_tool_registration.py`.

### Tool wiring

There is no registry module. `Ben/agents/factory.py` imports each tool module directly, builds `_BASE_TOOLS` / `_EXTRA_TOOLS` / `_COVERAGE_TOOLS` at import time (`:907-969`), and appends the per-branch finish tool. `register_agent_tools()` (`:984`) exists for extra tools and enforces globally unique tool names. shell tools (`exec_command`, `write_stdin`) are not hand-written: they come from the SDK Shell capability bound to the live sandbox session (`_make_shell_tools`, `:1008`). Every `FunctionTool` is wrapped by `_with_coerced_arguments` → `_with_strictness` → `_with_bounded_result`, and shell tools additionally by `_wrap_exec_command` (the whitelist gate) — so a new tool gets argument coercion, strict schemas, output truncation, and error-to-string behavior for free, but only shell tools get authorization checks. Anything that reaches the network outside `exec_command` must enforce the whitelist itself; the Caido proxy tools (`repeat_request` etc.) currently rely on the sandbox's dedicated Docker network + no-host-mount isolation rather than a hostname gate — worth knowing before you add another proxy-path tool.

Tool output is bounded and stored (`Ben/tools/output_store.py`); `Ben/tools/nullish.py` normalizes the various "no value" spellings models emit into `None`.

### Budget, pause, and steering

`Ben/core/hooks.py` separates two mechanisms. Budget limits use the coordinator's `budget_policy`, default `"stop"` (`Ben/core/agents.py:80`) — the agent raises `BudgetExceededError` and unwinds; a `"pause"` policy parks agents right before their next LLM call instead, and is exercised by `tests/test_budget_pause_policy.py` (currently nothing in the product sets it). Separately, an operator pause (`AgentCoordinator.pause_budget()`) parks every agent and `resume_budget()` wakes them; the runner drives this in-process by polling `BEN_SCAN_PAUSE_FILE` (`Ben/core/runner.py:1045`), which is how the out-of-process web console pauses a scan. Both paths are control flow, not failures: agents mid-call finish that call, and nothing is cancelled — so cost can slightly exceed the limit by design.

## Conventions and gotchas

- **Windows-first**: `.venv/Scripts/python.exe`, event-loop and fd quirks in `tests/test_session_fd.py`, UTF-8 stream forcing in `Ben/interface/main.py:_force_utf8_streams()`. On Windows the project directory must not carry a Low integrity label or the process cannot reach the Docker named pipe (README §11).
- **LiteLLM pricing**: `Ben/__init__.py` sets `LITELLM_LOCAL_MODEL_COST_MAP` for the whole package so offline runs don't stall on GitHub fetches; `tests/conftest.py` does the same before importing litellm. Don't rely on live price tables in tests.
- **Dependency manifests must agree**: `requirements.txt`, `pyproject.toml` `[project].dependencies`, and `uv.lock` are kept in lockstep; `tests/test_dependency_manifest.py` fails otherwise. CI also runs `uv lock --check`.
- **Tests assert on prompt/config text**, not just behavior: `tests/test_root_prompt_policy.py`, `tests/test_coverage_reconciliation_prompt.py`, `tests/test_dependency_manifest.py`. Editing `system_prompt.jinja` or the skill fragments can fail tests in non-obvious places.
- **`pytest.ini`** sets `asyncio_mode = auto` — async tests need no decorator; `testpaths = tests`.
- **`strix_runs/` and `reports/` are runtime output**, gitignored except `.gitkeep`. Don't treat a run directory in the tree as fixture data.
- Code comments and docstrings here are largely Chinese and carry design rationale (the "why", often with the upstream strix equivalent). Match that register when editing rather than converting to terse English.
- CI (`.github/workflows/ci.yml`) runs four jobs: Python 3.12 on Ubuntu+Windows (`uv sync --frozen`, lint, pytest), viewer (`npm ci`, typecheck, build), TUI (`go vet`, `go test -race`), and wheel packaging (installs the wheel and runs `ben --help` in a clean venv).
