import sys
sys.path.insert(0,'.')
from Ben.agents.prompt import _resolve_skills, render_system_prompt, split_cache_point
from Ben.skills import list_skills

skills = list_skills()
for mode in ("deep","standard","blackbox"):
    p = render_system_prompt(system_prompt_context={"target":"example.com"}, is_root=True, scan_mode=mode)
    prefix, suffix = split_cache_point(p)
    b = prefix.index("</available_skills>")+len("</available_skills>")
    a = prefix.rindex("<available_skills>",0,b)
    cat = len(prefix[a:b])
    resolved = _resolve_skills(None, scan_mode=mode, is_root=True, is_whitebox=False,
                              is_diff_scoped=False, report_tool="create_vulnerability_report",
                              coverage_tools=True)
    shared = sum(len(skills[q].body) for q in resolved)
    static = len(prefix) - cat - shared
    print(f"[{mode}] prefix={len(prefix)} chars (~{round(len(prefix)/4)} tok) = "
          f"static {static} (~{round(static/4)} tok) + shared-fragments {shared} (~{round(shared/4)} tok) "
          f"+ catalog {cat} (~{round(cat/4)} tok)")
    print(f"        catalog+shared = {round(100*(cat+shared)/len(prefix),1)}% of prefix")
