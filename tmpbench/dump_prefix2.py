import sys
sys.path.insert(0,'.')
from Ben.agents.prompt import render_system_prompt, split_cache_point
p = render_system_prompt(system_prompt_context={"target": "example.com"}, is_root=True, scan_mode="deep")
prefix, suffix = split_cache_point(p)
a = prefix.index("<available_skills>"); b = prefix.index("</available_skills>")+19
catalog = prefix[a:b]
open_ = catalog.count("<available_skills><available_skills>") if False else 0
print("occurrences of literal '<available_skills>':", catalog.count("<available_skills>"))
print("occurrences of '</available_skills>':", catalog.count("</available_skills>"))
idx = [i for i in range(len(catalog)) if catalog.startswith("<available_skills>", i)]
print("indices:", idx[:10])
# So we have two blocks: the tool doc mention + the real catalog
first = catalog.find("</available_skills>")
print("first block ends at:", first)
block2 = catalog[first:first+200]
print("repr around first close:", repr(block2[:200]))
