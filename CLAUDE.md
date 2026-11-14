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
python scripts/dev.py --help      # lists the tasks: test, lint, format-check, smoke, viewer, tui-*, package
```

`make` targets mirror these and `make help` lists them; on Windows the Makefile resolves `PY` to `.venv/Scripts/python.exe`.

### Running the product

```bash
# Self-tests, in order of increasing cost (the first two need no API key)
python main.py --selftest-docker     # container lifecycle, exit codes, timeouts
python main.py --selftest-tools      # real HTTP probe from inside the container
python main.py --selftest-llm        # real model call — needs .env

# A scan; README §八 has the full flag list
python main.py http://juice-shop:3000/          # interactive Go TUI when a terminal is attached
python main.py -n -t <url> -m quick --max-turns 20 --max-budget 1 --instruction "..."
python main.py -r <run_name>                    # resume a prior run from strix_runs/<name>/run.json

# React scan console (see "Interfaces")
python -m Ben.interface.web            # default port 8765, opens browser
python main.py view [<run>]            # read-only viewer for one run
```

`-n/--non-interactive` is what the web console passes; without it and with a TTY attached, `main()` hands off to the Go TUI and falls back to headless on TUI failure (the TUI branch in `Ben/interface/main.py:main()`).

Three settings name a sandbox image and they are **not** interchangeable: `Settings.executor_image` (`EXECUTOR_IMAGE`) is the one real scans use; `RuntimeSettings.image` (`BEN_IMAGE`) and the `DockerExecutor` constructor default belong to other code paths and pin different tags. Read the values from `Ben/config/settings.py` rather than trusting a tag copied into any doc. `make docker` / `scripts/docker.sh` builds a local `ben-sandbox:dev` from `containers/Dockerfile` instead.

## Architecture

### Model routing

`Ben/config/models.py` wraps the openai-agents SDK model and swaps in a LiteLLM-backed provider for any non-OpenAI prefix (`BenProvider._resolve_prefixed_model`) — a bare model name plus `LLM_API_BASE` goes straight to that endpoint, while `moonshot/...`, `openrouter/...`, `chatgpt/...` are rewritten to LiteLLM routes (the two modes are documented in README §五). The same module enforces streaming guards, turn limits, and repairs malformed tool-call payloads (`Ben/config/tool_call_arguments.py`, `tool_call_ids.py`, `tool_call_limits.py`) — provider-compatibility shims, not general utilities; keep them narrow.

Opt-in RAG: `Ben/rag/` (SQLite vector store, local or OpenAI embeddings) exposes `search_knowledge` into `_BASE_TOOLS` in `Ben/agents/factory.py`. LangChain is used only for splitting/embeddings here, never for orchestration.

### Control flow of one scan

`Ben/interface/main.py:main()` → `_run_task()` → `Ben/interface/cli/scan_setup.py:prepare_run()` → `Ben/core/runner.py:AgentRunner`:

1. `Settings` from `.env` (`Ben/config/settings.py`, pydantic-settings).
2. `AgentCoordinator` — the multi-agent graph: statuses, mailbox, `agents.json` snapshots (`Ben/core/agents.py`).
3. `Ben/runtime/session_manager.py:create_or_reuse()` — Docker sandbox session, optionally deferred while Caido sidecar starts.
4. `Ben/agents/factory.py:build_agent()` — root agent, plus a child factory that backs `create_agent`.
5. `Ben/core/execution.py:run_agent_loop()` → `_run_cycle()` — the actual SDK Runner loop, retries, transient-error recovery, budget parking.
6. Teardown → `AgentRunner._persist()` writes `reports/report_<stamp>.md` + `reports/trace_<stamp>.json`, archiving older ones into `reports/archive/YYYYMM/`.

The run record is also written incrementally: `strix_runs/<run_name>/run.json` (status, usage, targets), `.state/agents.json` + `.state/agents.db` (per-agent session history), plus `vulnerabilities.json`, `vulnerabilities.csv`, `findings.sarif`, `coverage.json`, `vulnerabilities/<id>.md`, and `penetration_test_report.md`. Both `Ben/core/paths.py` and the web viewer read that directory — it is the single source of truth for a run, live or finished. Note the two report surfaces are different files: the executive report inside the run dir (`penetration_test_report.md`) vs. the timestamped copies `_persist()` drops in `reports/` for the CLI (layout documented in README §九).

### Multi-agent model and the root-as-orchestrator split

`create_agent` / `wait_for_agents` / `send_message_to_agent` / `stop_agent` / `view_agent_graph` live in `Ben/tools/agents_graph/tools.py`; spawning is `spawn_child_agent()` in `Ben/core/execution.py`, which wraps each child in its own `asyncio` task in the same process. `agent_finish` (children) or `finish_scan` (root) terminates a branch.

The root agent is a **pure orchestrator**: `system_prompt.jinja` wraps the root in `<root_agent_directive>` telling it to delegate recon/testing/validation to children and *not* run scanners or probes itself (enforced by `tests/test_root_prompt_policy.py`). Every "you do X" instruction elsewhere in the prompt means "delegate X" when the reader is root. When changing prompt text or tool guidance, check which `is_root` branch you are in — the same paragraph reads differently for root and child.

### Prompt and skills assembly

`Ben/agents/prompt.py` renders a Jinja system prompt from `Ben/agents/prompts/` (main template + scope fragment) merged with reusable fragments from `Ben/skills/`.

- Skill names are `category/name`; bare names resolve across categories and raise on ambiguity (`Ben/skills/__init__.py`).
- Assembly order is fixed and deterministic so agents with identical parameters share an identical prompt prefix: user-requested → `scan_modes/<mode>` (+ `scan_modes/diff`) → reporting/coverage fragments → root fragment → whitebox fragment (`_resolve_skills`, `Ben/agents/prompt.py`). Prefix stability matters: a hoisted `<cache_point>` marker splits the immutable prefix from the variable suffix for prompt caching.
- `scan_mode` is one of `quick|standard|deep` (CLI `-m`, default `deep`); it selects a `scan_modes/<mode>.md` fragment. Whitebox is a separate axis, inferred from the target kinds — any `local_code` target (`is_whitebox_scan`, `Ben/interface/shared/targets.py`) turns it on and selects the `scan_modes/whitebox` fragment. The string `blackbox` is a *fragment* name, and `AgentRunner.scan_mode` defaults to `"blackbox"` even though the CLI never produces that value, so don't assume CLI `-m` and the fragment key share a namespace.
- `load_skill` (`Ben/tools/load_skill/tool.py`) lets a running agent pull fragment bodies on demand, max 5 per call.

Two skill directories exist and they are **not** interchangeable: `Ben/skills/` holds the fragments the renderer merges into system prompts, while `skills/` at the repo root holds the standalone Anthropic-style `SKILL.md` packages (unchanged from upstream) — code does not load those.

### Interfaces

- **Go TUI** (`Ben/interface/tui/`): real Go app, built to `Ben/bin/ben-tui.exe` (committed). Python side runs a JSON-over-socket backend server; protocol is versioned in `Ben/interface/tui/backend/protocol.py` (`PROTOCOL_VERSION`, capability list) and the Go side mirrors it in `internal/protocol`. Changing the wire format means bumping the version and touching both sides — `tests/test_tui_protocol_conformance.py` guards this. `Ben/interface/tui/sidecar.py` launches/supervises the binary and deliberately strips `*_API_KEY`/secret env vars before spawning it.
- **React web console** (`Ben/interface/viewer/`): stdlib `ThreadingHTTPServer` serving the prebuilt SPA from `Ben/interface/viewer/static/` (committed build output) plus a JSON API (`/api/runs`, `/api/run`, `/api/vulnerabilities`, `/api/report`, `/api/transcript`, `/api/report.pdf`, `POST /api/scans`). The page is authorized by a token in the operator link, which mints a session cookie (`Ben/interface/viewer/server.py`). `launcher.py` starts scans as a subprocess with `-n` and never through a shell. Source is `frontend/` (Vite + React 19 + Tailwind 4); after changing it run `make viewer` (or `python scripts/dev.py viewer`) and commit the rebuilt `static/` output — the server serves the committed bundle, not a dev server, so a source change without a rebuild is invisible at runtime.
- **Headless CLI** (`Ben/interface/cli/runner.py`): rich Live view, degrades to plain text when stdout is not a TTY or `-v` is set.

### Security gates (do not weaken casually)

The structural fact is that **authorization has exactly one chokepoint**: `_wrap_exec_command` in `Ben/agents/factory.py`, which calls `_check_command_allowed` → `_check_command_readonly` and resolves hosts via `_extract_hosts`. Gate coverage therefore equals "the tools wrapped by it" — in `_configure_shell_tools`, only `exec_command` is (its sibling `write_stdin` gets a separate wrapper; other tool families get none). Anything reaching the network by another route enforces nothing, and that is the question to ask before adding a network-capable tool.

- Whitelist check, twice: validated at startup (`_check_target_allowed` in `Ben/core/runner.py` for each prepared target) and again per command inside the wrapper. Shell-variable / command-substitution forms are rejected outright so the host cannot be resolved at execution time instead of check time.
- Read-only check: curl/wget/http/nc/socat may only issue GET/HEAD/OPTIONS; payload/upload flags are refused. Behavior-level description lives in README §十.
- MCP filesystem is configured read-only, with the write tools trimmed out (`~/.ben/mcp-servers.json`, `mcp_servers/filesystem/`), explicitly so an agent cannot rewrite `.env` and widen its own whitelist.
- The sandbox container is unprivileged, has no host mounts and no Docker socket, and runs on a dedicated network; `_warn_public_whitelist` logs when the whitelist contains public hosts.

Tests that encode these invariants: `tests/test_whitelist_gate.py`, `tests/test_agent_factory_shell.py`, `tests/test_agent_tool_registration.py`.

### Tool wiring

There is no registry module. `Ben/agents/factory.py` imports each tool module directly, builds `_BASE_TOOLS` / `_EXTRA_TOOLS` / `_COVERAGE_TOOLS` as module-level tuples, and appends the per-branch finish tool. `register_agent_tools()` exists for extra tools and enforces globally unique tool names. Shell tools (`exec_command`, `write_stdin`) are not hand-written: they come from the SDK Shell capability bound to the live sandbox session (`_make_shell_tools`). Every `FunctionTool` is wrapped by `_with_coerced_arguments` → `_with_strictness` → `_with_bounded_result`, with `_wrap_exec_command` / `_wrap_write_stdin` added for the shell pair — so a new tool gets argument coercion, strict schemas, output truncation, and error-to-string behavior for free, but only `exec_command` gets the authorization check. The Caido proxy tools (`repeat_request` etc.) reach the network through the sandbox's dedicated Docker network and no-host-mount isolation rather than a hostname gate.

Tool output is bounded and stored (`Ben/tools/output_store.py`); `Ben/tools/nullish.py` normalizes the various "no value" spellings models emit into `None`.

### Budget, pause, and steering

`Ben/core/hooks.py` separates two mechanisms. Budget limits use the coordinator's `budget_policy`, default `"stop"` (`AgentCoordinator` in `Ben/core/agents.py`) — the agent raises `BudgetExceededError` and unwinds; a `"pause"` policy parks agents right before their next LLM call instead, and is exercised by `tests/test_budget_pause_policy.py` (currently nothing in the product sets it). Separately, an operator pause (`AgentCoordinator.pause_budget()`) parks every agent and `resume_budget()` wakes them; the runner drives this in-process by polling `BEN_SCAN_PAUSE_FILE`, which is how the out-of-process web console pauses a scan. Both paths are control flow, not failures: agents mid-call finish that call, and nothing is cancelled — so cost can slightly exceed the limit by design.

## Conventions and gotchas

- **Windows-first**: `.venv/Scripts/python.exe`, event-loop and fd quirks in `tests/test_session_fd.py`, UTF-8 stream forcing in `Ben/interface/main.py:_force_utf8_streams()`. On Windows the project directory must not carry a Low integrity label or the process cannot reach the Docker named pipe (see the note near the end of README).
- **LiteLLM pricing**: `Ben/__init__.py` sets `LITELLM_LOCAL_MODEL_COST_MAP` for the whole package so offline runs don't stall on GitHub fetches; `tests/conftest.py` does the same before importing litellm. Don't rely on live price tables in tests.
- **Dependency manifests must agree**: `requirements.txt`, `pyproject.toml` `[project].dependencies`, and `uv.lock` are kept in lockstep (`tests/test_dependency_manifest.py`, plus `uv lock --check` in CI).
- **Tests assert on prompt/config text**, not just behavior: `tests/test_root_prompt_policy.py`, `tests/test_coverage_reconciliation_prompt.py`, `tests/test_dependency_manifest.py`. Editing `system_prompt.jinja` or the skill fragments can fail tests in non-obvious places.
- **`pytest.ini`** sets `asyncio_mode = auto` — async tests need no decorator; `testpaths = tests`.
- **`strix_runs/` and `reports/` are runtime output**, gitignored except `.gitkeep`. Don't treat a run directory in the tree as fixture data.
- Code comments and docstrings here are largely Chinese and carry design rationale (the "why", often with the upstream strix equivalent). Match that register when editing rather than converting to terse English.
- CI (`.github/workflows/ci.yml`) runs four jobs — Python (Ubuntu+Windows), viewer, TUI, wheel packaging. Read the workflow for the exact steps; it is the authority on what a PR must pass.
