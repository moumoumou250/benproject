import sys, time
sys.path.insert(0, "/home/kali/Desktop/AI/sapper-v2/lib")
import kb as kbmod
k = kbmod.KnowledgeBase("/home/kali/Desktop/AI/sapper-v2/kb")
t0=time.time()
for _ in range(20):
    k.search("smb relay ntlm")
t1=time.time()
print("20x kb.search: %.2fs -> %.1f ms/次" % (t1-t0, 1000*(t1-t0)/20))
t0=time.time()
for _ in range(20):
    k.list_entries()
t1=time.time()
print("20x list_entries: %.2fs" % (t1-t0))
