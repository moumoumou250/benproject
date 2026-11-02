
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, r"C:\Users\19219\WorkBuddy\BenProject")

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
sys.argv = ["main.py", "http://117.72.199.2:3000", "--instruction",
            "Mock 全流程验证：按工具返回执行即可。"]
start = time.time()
exit_code = 1
try:
    exit_code = main()
finally:
    server.shutdown()
print(f"\n[RESULT] exit={exit_code}, requests={len(CALLS)}, elapsed={time.time() - start:.1f}s")
sys.exit(exit_code if isinstance(exit_code, int) else 1)
