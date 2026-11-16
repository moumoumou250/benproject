import sys, time, tempfile, shutil, os
from pathlib import Path
sys.path.insert(0, "/home/kali/Desktop/AI/sapper-v2/lib")
import sapper

d = Path(tempfile.mkdtemp())
store = sapper.Store(d)
store.init(origin="box", goal="own it")
ids = []
t0 = time.time()
for i in range(120):
    f = store.add_fact(description=f"10.0.0.{i%250}:{8000+i} http nginx on web{i} admin panel", title=f"web{i}", creator="me", kind="service")
    ids.append(f["id"])
t1 = time.time()
for i in range(60):
    store.add_intent(from_ids=[ids[i], ids[i+1]], description=f"enumerate /admin on web{i} with dirb medium wordlist", creator="me")
t2 = time.time()
print("120 facts  : %.2fs (%.1f ms/fact)" % (t1-t0, 1000*(t1-t0)/120))
print("60 intents : %.2fs (%.1f ms/intent)" % (t2-t1, 1000*(t2-t1)/60))

# 文件写入量统计
import collections
writes = collections.Counter()
orig = sapper.atomic_write_json
def counting_write_json(path, payload):
    writes[Path(path).parent.name] += 1
    return orig(path, payload)
sapper.atomic_write_json = counting_write_json
for i in range(10):
    store.add_intent(from_ids=[ids[0]], description=f"probe task {i} aaaa", creator="me")
sapper.atomic_write_json = orig
print("10 add_intent -> writes:", dict(writes))

# recall 重复 SequenceMatcher
import difflib
orig_sm = difflib.SequenceMatcher
cnt = {"n": 0}
class CM(orig_sm):
    def __init__(self, *a, **k):
        cnt["n"] += 1
        super().__init__(*a, **k)
difflib.SequenceMatcher = CM
t0 = time.time(); sapper.recall(store, "admin panel web3"); t1 = time.time()
difflib.SequenceMatcher = orig_sm
print("1 recall over %d facts+intents: %.3fs, SequenceMatcher builds=%d" % (len(ids), t1-t0, cnt["n"]))
shutil.rmtree(d)
