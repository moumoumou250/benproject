import sys, json, tempfile, shutil
from pathlib import Path
sys.path.insert(0, "/home/kali/Desktop/AI/sapper-v2/lib")
import sapper

d = Path(tempfile.mkdtemp())
store = sapper.Store(d)
store.init(origin="box", goal="own it")
ids = []
for i in range(60):
    f = store.add_fact(description=f"10.0.0.{i%250}:{8000+i} http nginx 1.18.{i} on web{i}", title=f"web{i}", creator="me", kind="service", data={"port": str(8000+i)})
    ids.append(f["id"])
for i in range(40):
    store.add_intent(from_ids=[ids[i]], description=f"enumerate /admin on web{i} with dirb wordlist medium to find panels and default creds", creator="me", requires_confirmation=False)

g = sapper.build_graph_view(store)
yaml_txt = sapper._to_yaml_like(g)
fact_ids = json.dumps(g["fact_ids"], ensure_ascii=False, indent=2)
open_intents = json.dumps([sapper._intent_brief(i) for i in g["open_intents"]], ensure_ascii=False, indent=2)
hints = json.dumps([sapper._hint_brief(h) for h in g["hints"]], ensure_ascii=False, indent=2)

tpl = open("/home/kali/Desktop/AI/sapper-v2/templates/reason.md").read()
full = sapper.render_prompt(store, tpl)

print("facts=%d intents=%d" % (len(g["facts"]), len(g["intents"])))
print("template bytes        :", len(tpl))
print("graph_yaml bytes      :", len(yaml_txt))
print("fact_ids bytes        :", len(fact_ids))
print("open_intents bytes    :", len(open_intents))
print("hints bytes           :", len(hints))
print("rendered prompt bytes :", len(full))
dup = len(fact_ids) + len(open_intents)
print("redundant bytes       :", dup, "= %.1f%% of prompt" % (100*dup/len(full)))
print("graph_yaml share      : %.1f%%" % (100*len(yaml_txt)/len(full)))
print("static template share : %.1f%%" % (100*len(tpl)/len(full)))
print("approx tokens (b/3.6) :", int(len(full)/3.6))
shutil.rmtree(d)
