import sys
sys.path.insert(0,'.')
from Ben.agents.prompt import render_system_prompt, split_cache_point
for mode in ("deep","standard","blackbox"):
    p = render_system_prompt(system_prompt_context={"target":"example.com"}, is_root=True, scan_mode=mode)
    prefix, suffix = split_cache_point(p)
    b = prefix.index("</available_skills>")+len("</available_skills>")
    a = prefix.rindex("<available_skills>", 0, b)
    cat = prefix[a:b]
    print(f"[{mode}] total={len(p):6d} prefix={len(prefix):6d} (~{round(len(prefix)/4)} tok) "
          f"catalog={len(cat):6d} (~{round(len(cat)/4)} tok) share={round(100*len(cat)/len(prefix),1)}%")
    if mode=="deep":
        lines=[l for l in cat.split("\n") if l.startswith("- ")]
        print(f"        catalog entries={len(lines)} avg={round(sum(len(l)+1 for l in lines)/len(lines))} chars")
