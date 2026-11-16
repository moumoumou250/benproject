import sys
sys.path.insert(0,'.')
from Ben.agents.prompt import render_system_prompt, split_cache_point
p = render_system_prompt(system_prompt_context={"target": "example.com"}, is_root=True, scan_mode="deep")
prefix, suffix = split_cache_point(p)
a = prefix.index("<available_skills>"); b = prefix.index("</available_skills>")+19
catalog = prefix[a:b]
print("catalog lines:", catalog.count("\n")+1)
print("--- first 12 lines ---")
print("\n".join(catalog.split("\n")[:12])[:1500])
print("--- last 6 lines ---")
print("\n".join(catalog.split("\n")[-6:])[:1200])
print("--- entry line count (lines starting with '- ') ---")
lines=[l for l in catalog.split("\n") if l.startswith("- ")]
print(len(lines))
print("avg chars/entry:", round(sum(len(l)+1 for l in lines)/max(len(lines),1)))
