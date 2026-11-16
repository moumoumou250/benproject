import sys
sys.path.insert(0,'.')
from Ben.agents.prompt import _resolve_skills, render_system_prompt, split_cache_point
from Ben.skills import list_skills

skills = list_skills()
for mode in ("deep","standard","blackbox"):
    resolved = _resolve_skills(None, scan_mode=mode, is_root=True, is_whitebox=False,
                              is_diff_scoped=False, report_tool="create_vulnerability_report",
                              coverage_tools=True)
    tot = 0
    rows=[]
    for q in resolved:
        body = skills[q].body if hasattr(skills[q], "body") else ""
        rows.append((len(body), q))
        tot += len(body)
    print(f"[{mode}] shared inlined fragments: {len(resolved)}, total body chars={tot} (~{round(tot/4)} tok)")
    for L,q in sorted(rows, reverse=True)[:8]:
        print(f"    {L:6d}  {q}")
    print()
