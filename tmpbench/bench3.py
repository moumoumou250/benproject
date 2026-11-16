import sys, time, tempfile, shutil
from pathlib import Path
sys.path.insert(0, "/home/kali/Desktop/AI/sapper-v2/lib")
import sapper

d = Path(tempfile.mkdtemp())
store = sapper.Store(d)
store.init(origin="box", goal="own it")
ids = [store.add_fact(description=f"svc {i} host 10.0.0.{i} port {8000+i} http", title=f"h{i}", creator="me", kind="service")["id"] for i in range(150)]

# 1. list_facts vs 直接 path.exists 验证
t0=time.time()
for i in range(30):
    store._validate_from([ids[i]])
t1=time.time()
print("30x _validate_from (list_facts 全量)   : %.2fs" % (t1-t0))

def fast_validate(fids):
    for fid in fids:
        if not (store.facts_dir / f"{fid}.json").exists():
            raise AssertionError
t0=time.time()
for i in range(30):
    fast_validate([ids[i]])
t1=time.time()
print("30x path.exists 版本                    : %.2fs" % (t1-t0))

# 2. _next_id 写 project.json
t0=time.time()
for i in range(30):
    store._next_id("intent", "i")
t1=time.time()
print("30x _next_id (含 project.json 落盘)     : %.2fs" % (t1-t0))

# 3. 复用已有图快照算 frontier
g = sapper.build_graph_view(store)
t0=time.time()
for _ in range(20):
    sapper.build_graph_view(store)
t1=time.time()
print("20x build_graph_view                    : %.2fs" % (t1-t0))
t0=time.time()
for _ in range(20):
    sapper.frontier(store)
t1=time.time()
print("20x frontier (再扫一遍)                 : %.2fs" % (t1-t0))
shutil.rmtree(d)
