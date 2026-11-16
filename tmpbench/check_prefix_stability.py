"""Does the cacheable prefix stay byte-identical across agents/calls?"""
import sys, hashlib
sys.path.insert(0,'.')
from Ben.agents.prompt import render_system_prompt, split_cache_point

def h(s): return hashlib.sha256(s.encode()).hexdigest()[:12]

print("same inputs, two renders:")
p1 = render_system_prompt(system_prompt_context={"target":"example.com"}, is_root=True, scan_mode="deep")
p2 = render_system_prompt(system_prompt_context={"target":"example.com"}, is_root=True, scan_mode="deep")
print("  prefix hash A:", h(split_cache_point(p1)[0]), " B:", h(split_cache_point(p2)[0]))
print("  identical:", split_cache_point(p1)[0] == split_cache_point(p2)[0])

print("\nroot vs child (is_root=False):")
p3 = render_system_prompt(system_prompt_context={"target":"example.com"}, is_root=False, scan_mode="deep")
print("  root  prefix:", h(split_cache_point(p1)[0]))
print("  child prefix:", h(split_cache_point(p3)[0]))
print("  identical:", split_cache_point(p1)[0] == split_cache_point(p3)[0])
print("  root prefix len:", len(split_cache_point(p1)[0]), " child:", len(split_cache_point(p3)[0]))

print("\ndifferent target (volatile field inside prefix?):")
p4 = render_system_prompt(system_prompt_context={"target":"other.example.org"}, is_root=True, scan_mode="deep")
print("  prefix identical to example.com run:", split_cache_point(p1)[0] == split_cache_point(p4)[0])
