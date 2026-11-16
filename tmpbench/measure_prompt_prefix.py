import sys
sys.path.insert(0,'.')
from Ben.agents.prompt import render_system_prompt, split_cache_point

p = render_system_prompt(
    system_prompt_context={"target": "example.com"},
    is_root=True, scan_mode="deep",
)
prefix, suffix = split_cache_point(p)
print("rendered total chars:", len(p))
print("prefix chars:", len(prefix), "  est tokens:", round(len(prefix)/4))
print("suffix chars:", len(suffix), "  est tokens:", round(len(suffix)/4))
a = prefix.index("<available_skills>"); b = prefix.index("</available_skills>")+19
print("catalog chars:", b-a, "  est tokens:", round((b-a)/4))
print("catalog share of prefix:", round(100*(b-a)/len(prefix),1), "%")
print("prefix minus catalog chars:", len(prefix)-(b-a), " est tokens:", round((len(prefix)-(b-a))/4))
