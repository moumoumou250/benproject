import sys, json
sys.path.insert(0, '.')
from Ben.core.inputs import child_initial_input

# Build a synthetic but realistic parent history: 30 turns, tool call + large tool output each.
items = []
items.append({"role": "user", "content": "Scan example.com in blackbox mode. Goals: find auth bypass, IDOR, SSRF."})
for i in range(20):
    items.append({"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "nmap -sV example.com"}), "call_id": f"c{i}"})
    body = ("PORT     STATE SERVICE\n80/tcp   open  http\n443/tcp  open  https\n"
            + ("#" * 400) + f"\nphase {i} output line\n" * 30)
    items.append({"type": "function_call_output", "call_id": f"c{i}", "output": body})
    items.append({"role": "assistant", "content": [{"type": "output_text", "text": "Observed open ports; next I will probe the login form for default credentials and test the redirect parameter for open redirect."}]})

msg = child_initial_input(name="web-prober", child_id="ab12cd34", parent_id="root0001",
                          task="Probe /login for default creds; test ?next= for open redirect.",
                          parent_history=items)
content = msg[0]["content"]
raw = json.dumps(items, ensure_ascii=False, default=str)
print("history items:", len(items))
print("raw json.dumps(parent_history) chars:", len(raw))
print("rendered child_initial_input chars    :", len(content))
print("overhead of wrapper                   :", len(content) - len(raw))
print("est tokens (4 chars/token)            :", round(len(content)/4))
# A sapper-style compact block for the same history
compact = ("== Inherited context (background only) ==\n"
           "facts:\n- [f1] 80/443 open, nginx 1.18 (phase 0-19)\n"
           "- [f2] /login accepts POST, no CAPTCHA\n"
           "- [f3] ?next= reflected unvalidated\n"
           "open intents:\n- probe /login default creds -> creds_valid\n- test next= open redirect -> redirect_open\n"
           "docs: not inlined; ask parent or read .ben/facts/<id> for full text.\n")
print("sapper-style rendered chars           :", len(compact))
print("saving per child                      :", len(content)-len(compact), "chars =",
      round((1-len(compact)/len(content))*100,1), "%")
