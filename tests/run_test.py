#!/usr/bin/env python3
"""BenProject 低成本测试启动器 —— 一条命令跑完 0~3 层验证。

用法（在项目根目录）：

    .venv/Scripts/python.exe test/run_test.py              # 全自动：0→1→2→2.5→3 层
    .venv/Scripts/python.exe test/run_test.py --layer 0    # 只跑第 0 层（¥0）
    .venv/Scripts/python.exe test/run_test.py --layer 2    # 只跑到第 2 层（¥0.007）
    .venv/Scripts/python.exe test/run_test.py --layer 3    # 跑到第 3 层（¥0.5~2）
    .venv/Scripts/python.exe test/run_test.py --skip-docker  # 跳过需要 Docker 的层
    .venv/Scripts/python.exe test/run_test.py --skip-mock    # 跳过第 2.5 层假模型

设计思路：
- 第 0 层（¥0）：语法编译 + 导入冒烟 + CLI 接线 + 配置解析 + 提示词渲染 + 工具 schema
- 第 1 层（¥0）：Docker 执行器 + 工具层真实执行（需 Docker）
- 第 2 层（¥0.007）：真模型连通性（24h 内配置未变则命中缓存，¥0）
- 第 2.5 层（¥0）：假模型全流程 —— 本地 mock OpenAI server 按脚本返回 tool_call，
  验证 runner 循环 / 工具分发 / hooks / 报告生成，不调真模型、不需要 Docker。
  mock 层不绿就不该烧真钱跑第 3 层
- 第 3 层（¥0.5~2）：迷你真跑 —— MAX_AGENT_TURNS=2 + 硬摘 create_agent/stop_agent
  + 预算止损（max_budget_usd=$0.5）+ 硬工具调用上限（12 次），四重保险锁成本
  （预算止损依赖 pricing 表，可能静默失效；工具计数是物理量，一定拦得住）

不修改项目任何文件，全部通过环境变量 + 运行时 monkey-patch 实现。
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

# 本文件在 test/ 子目录，项目根是上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
TARGET_URL = "http://117.72.199.2:3000"
BUDGET_USD = 0.5  # 第 3 层预算硬止损
TOOL_CALL_LIMIT = 12  # 第 3 层硬工具调用上限（第四重保险）
L2_CACHE_FILE = TEST_DIR / ".cache" / "l2_ok.json"
L2_CACHE_TTL = 24 * 3600  # L2 连通性缓存 24h

# ── 颜色输出 ──────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def header(title: str) -> None:
    print(f"\n{CYAN}{BOLD}{'=' * 60}{RESET}")
    print(f"{CYAN}{BOLD}  {title}{RESET}")
    print(f"{CYAN}{BOLD}{'=' * 60}{RESET}")


def ok(msg: str) -> None:
    print(f"  {GREEN}[OK]{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}[FAIL]{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}[WARN]{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {CYAN}[INFO]{RESET} {msg}")


def run_cmd(cmd: list[str], *, timeout: int = 120, env: dict | None = None) -> tuple[int, str, str]:
    """运行命令，返回 (exit_code, stdout, stderr)。"""
    merged_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # Windows GBK 控制台下中文输出防 UnicodeDecodeError
            timeout=timeout,
            env=merged_env,
            cwd=str(PROJECT_ROOT),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"超时（{timeout}s）"
    except FileNotFoundError:
        return -1, "", f"命令不存在：{cmd[0]}"


# ── 第 0 层：零 token 接线检查 ────────────────────────────────────
def layer0() -> bool:
    header("第 0 层：零 token 接线检查（¥0）")
    py = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    all_ok = True

    # 0-1: 全量语法编译
    code, out, err = run_cmd([py, "-m", "compileall", "-q", "Ben"], timeout=60)
    if code == 0:
        ok("全量语法编译通过")
    else:
        fail(f"语法编译失败：{err[:200]}")
        all_ok = False

    # 0-2: 导入冒烟（核心模块）
    import_cmds = [
        "import Ben.config",
        "import Ben.agents.factory",
        "import Ben.agents.prompt",
        "import Ben.core.runner",
        "import Ben.core.execution",
        "import Ben.tools.agents_graph.tools",
        "import Ben.tools.finish.tool",
        "import Ben.tools.mcp.agent_tools",
        "import Ben.interface.main",
    ]
    code, out, err = run_cmd(
        [py, "-c", "; ".join(import_cmds)],
        timeout=30,
    )
    if code == 0:
        ok("核心模块导入冒烟通过（9 个模块）")
    else:
        fail(f"导入失败：{err[:300]}")
        all_ok = False

    # 0-3: CLI 接线（匹配完整参数名，"-m" 之类的短串会误判）
    code, out, err = run_cmd([py, "main.py", "--help"], timeout=15)
    if code == 0 and ("--selftest-llm" in out or "--selftest-docker" in out):
        ok("CLI --help 正常，参数已注册")
    else:
        fail(f"CLI 接线异常：{err[:200]}")
        all_ok = False

    # 0-4: 配置解析（不打印 key）
    config_check = (
        "from Ben.config import load_settings; "
        "s = load_settings(require_api_key=False); "
        "print(f'model={s.llm.model}'); "
        "print(f'api_base={s.llm.api_base}'); "
        "print(f'targets={s.allowed_targets}'); "
        "print(f'timeout={s.command_timeout}'); "
        "print(f'max_turns={s.llm.max_turns}')"
    )
    code, out, err = run_cmd([py, "-c", config_check], timeout=15)
    if code == 0 and "model=" in out:
        ok(f"配置解析通过：{out.strip().replace(chr(10), ' | ')}")
    else:
        fail(f"配置解析失败：{err[:200]}")
        all_ok = False

    # 0-5: 提示词渲染矩阵（8 种组合）
    prompt_check = (
        "from Ben.agents.prompt import render_system_prompt, build_system_prompt_context; "
        "from Ben.config import load_settings; "
        "s = load_settings(require_api_key=False); "
        "ctx = build_system_prompt_context(s, str(s.allowed_targets[0])); "
        "combos = ["
        "  (False, False, False, False),"
        "  (False, False, False, True),"
        "  (True, False, False, False),"
        "  (True, False, False, True),"
        "  (False, True, False, False),"
        "  (False, True, False, True),"
        "  (True, True, True, False),"
        "  (True, True, True, True),"
        "]; "
        "results = []; "
        "[results.append(render_system_prompt("
        "    requested_skills=[], scan_mode='whitebox' if w else 'blackbox', "
        "    is_root=r, is_whitebox=w, is_diff_scoped=d, "
        "    interactive=False, parallel_tool_calls=False, "
        "    system_prompt_context=ctx, supports_images=False, "
        "    report_tool=True, coverage_tools=c"
        ")) for r, w, d, c in combos]; "
        "nonempty = sum(1 for s in results if len(s) > 0); "
        "print(f'rendered {nonempty}/{len(results)} prompts non-empty, all_ok={nonempty == len(results)}')"
    )
    code, out, err = run_cmd([py, "-c", prompt_check], timeout=15)
    if code == 0 and "all_ok=True" in out:
        ok("提示词渲染矩阵通过（8 种组合全部非空）")
    else:
        fail(f"提示词渲染失败：{err[:300]}")
        all_ok = False

    # 0-6: 工具 schema 自校验
    schema_check = (
        "import json; "
        "from jsonschema import Draft202012Validator; "
        "from Ben.agents.factory import _BASE_TOOLS; "
        "from Ben.tools.finish.tool import finish_scan; "
        "all_tools = [*_BASE_TOOLS, finish_scan]; "
        "passed = 0; "
        "["
        "  (Draft202012Validator.check_schema(t.params_json_schema), passed := passed + 1)"
        "  for t in all_tools"
        "  if hasattr(t, 'params_json_schema') and t.params_json_schema"
        "]; "
        "print(f'schema validated: {passed} tools')"
    )
    code, out, err = run_cmd([py, "-c", schema_check], timeout=15)
    if code == 0 and "schema validated" in out:
        ok(f"工具 schema 校验通过：{out.strip()}")
    else:
        # jsonschema 可能没装，降级为 json.dumps 检查
        simple_check = (
            "import json; "
            "from Ben.agents.factory import _BASE_TOOLS; "
            "from Ben.tools.finish.tool import finish_scan; "
            "all_tools = [*_BASE_TOOLS, finish_scan]; "
            "count = sum(1 for t in all_tools if hasattr(t, 'params_json_schema') and t.params_json_schema and json.loads(json.dumps(t.params_json_schema)) is not None); "
            "print(f'json-serializable schemas: {count}/{len(all_tools)} tools')"
        )
        code2, out2, err2 = run_cmd([py, "-c", simple_check], timeout=15)
        if code2 == 0:
            ok(f"工具 schema JSON 序列化通过：{out2.strip()}")
        else:
            fail(f"工具 schema 校验失败：{err[:200]}")
            all_ok = False

    # 0-7: 提示词渲染中 quick/standard 模式片段缺失告警
    skill_check = (
        "from pathlib import Path; "
        "from Ben.config.settings import PROJECT_ROOT; "
        "modes_dir = PROJECT_ROOT / 'Ben' / 'skills' / 'scan_modes'; "
        "existing = sorted([f.stem for f in modes_dir.glob('*.md')]) if modes_dir.exists() else []; "
        "print(f'scan_modes: {existing}')"
    )
    code, out, err = run_cmd([py, "-c", skill_check], timeout=10)
    if code == 0:
        ok(f"模式片段清单：{out.strip()}")
        if "quick" not in out:
            warn("-m quick 会静默降级（无 quick.md 片段），不影响功能但也不省钱")
    else:
        warn(f"模式片段检查跳过：{err[:100]}")

    if all_ok:
        print(f"\n  {GREEN}{BOLD}第 0 层全部通过 ✓{RESET}")
    else:
        print(f"\n  {RED}{BOLD}第 0 层有失败项，请修复后再继续 ✗{RESET}")
    return all_ok


# ── 第 1 层：Docker + 工具层（¥0）─────────────────────────────────
def layer1() -> bool:
    header("第 1 层：Docker 执行器 + 工具层（¥0，需 Docker）")
    py = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

    # 1-1: Docker 执行器自检
    info("运行 --selftest-docker（拉镜像/起容器/执行，不调模型）...")
    code, out, err = run_cmd([py, "main.py", "--selftest-docker"], timeout=180)
    if code == 0 and "ALL OK" in out:
        ok("Docker 执行器自检通过")
    else:
        fail(f"Docker 执行器自检失败（exit={code}）")
        if err:
            info(f"stderr: {err[:300]}")
        if out:
            info(f"stdout: {out[-300:]}")
        warn("Docker 未就绪——第 1/3 层都依赖它，请先启动 Docker Desktop")
        return False

    # 1-2: 工具层真实执行
    info("运行 --selftest-tools（容器内对授权目标 curl，不调模型）...")
    code, out, err = run_cmd([py, "main.py", "--selftest-tools"], timeout=60)
    if code == 0 and "ALL OK" in out:
        ok("工具层真实执行通过")
    else:
        fail(f"工具层执行失败（exit={code}）")
        if err:
            info(f"stderr: {err[:300]}")
        return False

    print(f"\n  {GREEN}{BOLD}第 1 层全部通过 ✓{RESET}")
    return True


# ── 第 2 层：真模型连通（≈¥0.007，24h 缓存）───────────────────────
def _l2_cache_key(py: str) -> str | None:
    """按 (model, api_base, api_key) 算缓存 key——只输出哈希，不泄露 key。"""
    probe = (
        "from Ben.config.loader import load_settings; "
        "import hashlib; "
        "s = load_settings(); "
        "print(hashlib.sha256("
        "f'{s.llm.model}|{s.llm.api_base}|{s.llm.api_key}'.encode()"
        ").hexdigest())"
    )
    code, out, err = run_cmd([py, "-c", probe], timeout=15)
    return out.strip() if code == 0 and out.strip() else None


def _l2_cache_hit(key: str | None) -> bool:
    if not key or not L2_CACHE_FILE.exists():
        return False
    try:
        data = json.loads(L2_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("key") == key and (time.time() - data.get("ts", 0)) < L2_CACHE_TTL


def _l2_cache_store(key: str | None) -> None:
    if not key:
        return
    try:
        L2_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        L2_CACHE_FILE.write_text(json.dumps({"key": key, "ts": time.time()}), encoding="utf-8")
    except Exception:
        pass


def layer2() -> bool:
    header("第 2 层：真模型连通性验证（≈¥0.007，24h 内配置未变则免费跳过）")
    py = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

    key = _l2_cache_key(py)
    if _l2_cache_hit(key):
        ok("命中 24h 缓存（model/api_base/key 未变），跳过真模型调用，省 ¥0.007")
        return True

    info("运行 --selftest-llm（发一条最小请求，验证 Key/接口/模型）...")
    code, out, err = run_cmd([py, "main.py", "--selftest-llm"], timeout=60)
    if code == 0 and "ALL OK" in out:
        ok("模型文本调用正常")
        _l2_cache_store(key)
        # 提取模型回复
        for line in out.splitlines():
            if "模型回复" in line:
                info(line.strip())
                break
        print(f"\n  {GREEN}{BOLD}第 2 层全部通过 ✓{RESET}")
        return True
    else:
        fail(f"模型连通失败（exit={code}）")
        if err:
            info(f"stderr: {err[:300]}")
        if out:
            info(f"stdout: {out[-300:]}")
        warn("检查 .env 中的 LLM_API_KEY / LLM_API_BASE / LLM_MODEL")
        return False


# ── 第 2.5 层：假模型全流程（¥0，无需 Docker）─────────────────────
_MOCK_SCRIPT = r'''
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, r"@PROJECT_ROOT@")

CALLS = []

def _tool_call(name, args, idx):
    return {"id": f"call_mock_{idx}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        n = len(CALLS)
        CALLS.append(n)
        # 脚本化剧情：第 1 轮调 list_todos（无参、纯本地），第 2 轮 finish_scan 收尾
        if n == 0:
            message = {"role": "assistant", "content": None,
                       "tool_calls": [_tool_call("list_todos", {}, n)]}
            finish = "tool_calls"
        elif n == 1:
            message = {"role": "assistant", "content": None,
                       "tool_calls": [_tool_call("finish_scan", {
                           "executive_summary": "Mock 执行摘要：流水线接线验证。",
                           "methodology": "Mock 方法论：脚本化 tool_call。",
                           "technical_analysis": "Mock 技术分析：未执行真实扫描。",
                           "recommendations": "Mock 建议：无需操作。",
                       }, n)]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "[MOCK] 额外轮次，直接结束。"}
            finish = "stop"
        payload = json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion",
            "created": int(time.time()), "model": "mock-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

# 把 LLM 指向本地 mock——必须在任何 Ben import 之前设置（settings 有 memoize）
os.environ["LLM_API_BASE"] = f"http://127.0.0.1:{port}/v1"
os.environ["LLM_API_KEY"] = "mock-key"
os.environ["LLM_MODEL"] = "openai/gpt-4o-mini"   # 强制 openai 路由，避开 codex 订阅通道
os.environ["BEN_API_TYPE"] = "chat_completions"  # 强制 chat completions，mock 只实现这个端点
os.environ["LLM_DISABLE_STREAMING"] = "1"        # mock 只返回非流式 JSON
os.environ["MAX_AGENT_TURNS"] = "5"

# mock 层不派生子 Agent
from Ben.agents import factory as _fac
from Ben.tools.agents_graph.tools import create_agent, stop_agent
_fac._BASE_TOOLS = tuple(t for t in _fac._BASE_TOOLS if t not in (create_agent, stop_agent))
print(f"[MOCK] server on 127.0.0.1:{port}, _BASE_TOOLS={len(_fac._BASE_TOOLS)}", flush=True)

from Ben.interface.main import main
sys.argv = ["main.py", "@TARGET_URL@", "--instruction",
            "Mock 全流程验证：按工具返回执行即可。"]
start = time.time()
exit_code = 1
try:
    exit_code = main()
finally:
    server.shutdown()
print(f"\n[RESULT] exit={exit_code}, requests={len(CALLS)}, elapsed={time.time() - start:.1f}s")
sys.exit(exit_code if isinstance(exit_code, int) else 1)
'''


def layer2_5() -> bool:
    header("第 2.5 层：假模型全流程（¥0，无需 Docker）")
    py = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

    info("本地 mock OpenAI server 按脚本返回 tool_call：list_todos → finish_scan")
    info("验证 runner 循环 / 工具分发 / hooks / 报告生成——全程不调真模型")
    info("此层不绿就不该烧真钱跑第 3 层")

    script = _MOCK_SCRIPT.replace("@PROJECT_ROOT@", str(PROJECT_ROOT)).replace("@TARGET_URL@", TARGET_URL)
    script_path = PROJECT_ROOT / "_test_mock_layer.py"
    script_path.write_text(script, encoding="utf-8")

    try:
        code, out, err = run_cmd([py, str(script_path)], timeout=300)

        requests = 0
        for line in out.splitlines():
            if "[MOCK]" in line or "[RESULT]" in line or "报告文件" in line:
                print(f"  {line}")
            if "requests=" in line:
                try:
                    requests = int(line.split("requests=")[1].split(",")[0].strip())
                except (ValueError, IndexError):
                    pass
        if err:
            for line in err.splitlines():
                if any(kw in line for kw in ["ERROR", "FAIL", "Traceback", "Error"]):
                    print(f"  {line}")

        if code == 0 and requests >= 2:
            ok(f"假模型全流程通过（mock 收到 {requests} 次请求，runner 循环/工具分发/收尾全走通）")
            print(f"\n  {GREEN}{BOLD}第 2.5 层全部通过 ✓{RESET}")
            return True
        fail(f"假模型全流程失败（exit={code}, requests={requests}）")
        if err:
            info(f"stderr 末尾：{err[-400:]}")
        warn("先修好这里再跑第 3 层——mock 都过不了，真模型只会烧钱报错")
        return False
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── 第 3 层：迷你真跑（¥0.5~2，四重保险）──────────────────────────
_LAYER3_SCRIPT = r'''
import os, sys, time
sys.path.insert(0, r"@PROJECT_ROOT@")

# 保险 1：压低轮数（进程环境变量优先级高于 .env）
os.environ["MAX_AGENT_TURNS"] = "2"

# 保险 2：硬摘 create_agent / stop_agent（root 无法派生子 Agent）
from Ben.agents import factory as _fac
from Ben.tools.agents_graph.tools import create_agent, stop_agent
_original_base = _fac._BASE_TOOLS
_fac._BASE_TOOLS = tuple(t for t in _original_base if t not in (create_agent, stop_agent))
print(f"[PATCH] _BASE_TOOLS: {len(_original_base)} -> {len(_fac._BASE_TOOLS)}（已摘 create_agent/stop_agent）", flush=True)

# 保险 3：预算止损（ReportUsageHooks 注入 max_budget_usd）
# runner.py 做 `from Ben.core.hooks import ReportUsageHooks`，
# 拿到的是同一个类对象引用——patch __init__ 即可全局生效。
# 注意：成本统计依赖 pricing 表认识该模型，否则 cost=0、此保险静默失效——所以还有保险 4。
from Ben.core import hooks as _hooks
_orig_init = _hooks.ReportUsageHooks.__init__
def _patched_init(self, *, model, max_budget_usd=None, **kw):
    if max_budget_usd is None:
        max_budget_usd = @BUDGET_USD@
    print(f"[PATCH] ReportUsageHooks max_budget_usd=${max_budget_usd}", flush=True)
    _orig_init(self, model=model, max_budget_usd=max_budget_usd, **kw)
_hooks.ReportUsageHooks.__init__ = _patched_init

# 保险 4：硬工具调用上限（物理计数，不依赖成本统计，一定拦得住）
_TOOL_LIMIT = @TOOL_LIMIT@
_orig_on_tool_end = _hooks.ReportUsageHooks.on_tool_end
async def _counting_on_tool_end(self, context, agent, tool, result):
    count = getattr(self, "_test_tool_calls", 0) + 1
    self._test_tool_calls = count
    if count > _TOOL_LIMIT:
        raise RuntimeError(f"[TEST] 硬工具调用上限 {_TOOL_LIMIT} 次已超出，强制中止")
    return await _orig_on_tool_end(self, context, agent, tool, result)
_hooks.ReportUsageHooks.on_tool_end = _counting_on_tool_end
print(f"[PATCH] 硬工具调用上限 = {_TOOL_LIMIT} 次", flush=True)

# 自证保险 1 真的生效（loader 有 memoize，但 env 在 import 前已设置）
from Ben.config.loader import load_settings as _load
print(f"[PATCH] max_turns 生效值 = {_load().llm.max_turns}（必须为 2）", flush=True)

# 运行迷你任务
from Ben.interface.main import main
sys.argv = [
    "main.py",
    "@TARGET_URL@",
    "--instruction",
    "只做两件事：抓一次首页、做一次目录探测，然后立刻调用 finish_scan。不要派生任何子 Agent。",
]
start = time.time()
exit_code = main()
elapsed = time.time() - start
print(f"\n[RESULT] exit={exit_code}, elapsed={elapsed:.1f}s")
sys.exit(exit_code)
'''


def _target_reachable() -> bool:
    """免费预检：目标不可达就不烧第 3 层的钱。HTTP 错误码也算可达（服务器有响应）。"""
    try:
        req = urllib.request.Request(TARGET_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def layer3() -> bool:
    header(f"第 3 层：迷你真跑（预算上限 ${BUDGET_USD}，MAX_TURNS=2，无子 Agent，工具上限 {TOOL_CALL_LIMIT} 次）")
    py = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

    # Docker 状态由第 1 层保证，这里不重复自检（省 120s）
    info("预检目标可达性（免费 HTTP HEAD）...")
    if not _target_reachable():
        fail(f"目标不可达：{TARGET_URL}，跳过第 3 层（未产生费用）")
        return False
    ok("目标可达")

    info("四重成本保险：")
    info("  1) MAX_AGENT_TURNS=2（环境变量，进程级覆盖 .env）")
    info("  2) 硬摘 create_agent/stop_agent（monkey-patch _BASE_TOOLS）")
    info(f"  3) max_budget_usd=${BUDGET_USD}（monkey-patch ReportUsageHooks）")
    info(f"  4) 硬工具调用上限 {TOOL_CALL_LIMIT} 次（物理计数，pricing 失效也拦得住）")

    script = (
        _LAYER3_SCRIPT
        .replace("@PROJECT_ROOT@", str(PROJECT_ROOT))
        .replace("@TARGET_URL@", TARGET_URL)
        .replace("@BUDGET_USD@", repr(BUDGET_USD))
        .replace("@TOOL_LIMIT@", str(TOOL_CALL_LIMIT))
    )
    patch_path = PROJECT_ROOT / "_test_layer3_patch.py"
    patch_path.write_text(script, encoding="utf-8")

    try:
        info(f"启动迷你真跑（目标 {TARGET_URL}）...")
        info("预计耗时 1-3 分钟，预计花费 ¥0.5-2")
        code, out, err = run_cmd(
            [py, str(patch_path)],
            timeout=600,  # 10 分钟超时
        )

        # 输出关键信息
        for line in out.splitlines():
            if any(kw in line for kw in ["[PATCH]", "[RESULT]", "报告文件", "轨迹文件", "结束原因", "工具调用"]):
                print(f"  {line}")

        if err:
            for line in err.splitlines():
                if any(kw in line for kw in ["ERROR", "FAIL", "阶段", "Turn", "工具调用"]):
                    print(f"  {line}")

        if code == 0:
            ok("迷你真跑完成")
            # 查找生成的报告
            reports_dir = PROJECT_ROOT / "reports"
            if reports_dir.exists():
                reports = sorted(reports_dir.glob("report_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
                if reports:
                    info(f"最新报告：{reports[0].name}")
            print(f"\n  {GREEN}{BOLD}第 3 层完成 ✓{RESET}")
            print(f"  {YELLOW}如果报告内容合理，说明整条流水线验证通过。{RESET}")
            print(f"  {YELLOW}后续可考虑全量真跑（¥7-11/次）。{RESET}")
            return True
        else:
            fail(f"迷你真跑失败（exit={code}）")
            if err:
                info(f"stderr 末尾：{err[-400:]}")
            return False
    finally:
        # 清理临时文件
        try:
            patch_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── 主流程 ────────────────────────────────────────────────────────
def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="BenProject 低成本测试启动器")
    parser.add_argument(
        "--layer", type=int, default=3, choices=[0, 1, 2, 3],
        help="跑到第几层就停（默认 3：全跑）",
    )
    parser.add_argument("--skip-docker", action="store_true", help="跳过需要 Docker 的层")
    parser.add_argument("--skip-mock", action="store_true", help="跳过第 2.5 层假模型全流程")
    args = parser.parse_args()

    print(f"{BOLD}BenProject 低成本测试启动器{RESET}")
    print(f"  目标：{TARGET_URL}")
    print(f"  策略：先免费接线 → 再几分钱连通 → 假模型全流程 → 最后一两块钱迷你真跑")
    print(f"  预算：第 3 层硬止损 ${BUDGET_USD}（≈¥3.5）+ 硬工具上限 {TOOL_CALL_LIMIT} 次")

    results: dict[str, bool] = {}

    # 第 0 层（总是跑）
    results["L0"] = layer0()
    if not results["L0"] or args.layer < 1:
        _print_summary(results)
        return 0 if all(results.values()) else 1

    # 第 1 层（需要 Docker）
    if args.skip_docker:
        warn("跳过第 1 层（--skip-docker）")
        results["L1"] = True  # 假定通过
    else:
        results["L1"] = layer1()
    if not results["L1"] or args.layer < 2:
        _print_summary(results)
        return 0 if all(results.values()) else 1

    # 第 2 层
    results["L2"] = layer2()
    if not results["L2"] or args.layer < 3:
        _print_summary(results)
        return 0 if all(results.values()) else 1

    # 第 2.5 层：假模型全流程——不绿就不烧第 3 层的钱
    if args.skip_mock:
        warn("跳过第 2.5 层（--skip-mock）")
        results["L2.5"] = True
    else:
        results["L2.5"] = layer2_5()
    if not results["L2.5"]:
        _print_summary(results)
        return 1

    # 第 3 层
    results["L3"] = layer3()

    _print_summary(results)
    return 0 if all(results.values()) else 1


def _print_summary(results: dict[str, bool]) -> None:
    header("总结")
    layer_names = {
        "L0": "第 0 层：零 token 接线检查（¥0）",
        "L1": "第 1 层：Docker + 工具层（¥0）",
        "L2": "第 2 层：真模型连通（≈¥0.007，可缓存）",
        "L2.5": "第 2.5 层：假模型全流程（¥0）",
        "L3": "第 3 层：迷你真跑（¥0.5-2）",
    }
    for key in ["L0", "L1", "L2", "L2.5", "L3"]:
        if key in results:
            status = f"{GREEN}✓ PASS{RESET}" if results[key] else f"{RED}✗ FAIL{RESET}"
            print(f"  {layer_names[key]:<45} {status}")
        else:
            print(f"  {layer_names[key]:<45} {YELLOW}— SKIP{RESET}")

    all_pass = all(results.values()) if results else False
    if all_pass:
        print(f"\n  {GREEN}{BOLD}所有层通过！项目流水线验证完成。{RESET}")
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"\n  {RED}{BOLD}失败层：{', '.join(failed)}{RESET}")
        print(f"  {YELLOW}建议：在失败的层停下修，不要往下烧钱。{RESET}")


if __name__ == "__main__":
    sys.exit(main())
