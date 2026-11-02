"""对已发现的三个信息暴露点做只读取证细化。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests

BASE = "http://117.72.199.2:3000"
OUT = Path(__file__).resolve().parents[2] / "reports"
s = requests.Session()
s.headers["User-Agent"] = "BenPT-readonly-audit/1.0"


def head(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# 1) /ftp 目录列表：暴露了哪些文件
head("1) /ftp 目录列表暴露的文件")
r = s.get(BASE + "/ftp", timeout=15)
entries = sorted(set(re.findall(r'>([A-Za-z0-9_.\-]+)</a>', r.text)))
print(f"HTTP {r.status_code}, {len(entries)} 个条目:")
for e in entries:
    print(f"  - {e}")

# 2) application-configuration：哪些键被未授权读取
head("2) /rest/admin/application-configuration 顶层结构")
cfg = s.get(BASE + "/rest/admin/application-configuration", timeout=15).json()
top = sorted(cfg.keys())
print(f"HTTP 200, 顶层键 {len(top)} 个: {top}")
config_keys = cfg.get("config", {}) if isinstance(cfg.get("config"), dict) else {}
print(f"\nconfig 段键: {sorted(config_keys.keys())}")
for probe in ["googleOAuth", "application", "challenges", "hackingInstructor", "ctf"]:
    if probe in config_keys:
        v = config_keys[probe]
        txt = json.dumps(v, ensure_ascii=False)
        print(f"  {probe}: {txt[:220]}")

# 3) /metrics：暴露了哪些指标族
head("3) /metrics 指标族")
m = s.get(BASE + "/metrics", timeout=15)
families = sorted(set(re.findall(r"^# TYPE (\S+)", m.text, re.M)))
print(f"HTTP {m.status_code}, 指标族 {len(families)} 个:")
for f in families:
    print(f"  - {f}")

# 4) /api/Feedbacks：评论中的邮箱
head("4) /api/Feedbacks 评论中的邮箱暴露")
fb = s.get(BASE + "/api/Feedbacks", timeout=15).json()
emails = set()
for row in fb.get("data", []):
    for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", str(row.get("comment", ""))):
        emails.add(e)
print(f"HTTP 200, {len(fb.get('data', []))} 条反馈, 评论内邮箱 {len(emails)} 个: {sorted(emails)}")

OUT.mkdir(exist_ok=True)
(OUT / "exposure_detail.json").write_text(json.dumps({
    "ftp_entries": entries,
    "config_top_keys": top,
    "config_section_keys": sorted(config_keys.keys()),
    "metrics_families": families,
    "feedback_emails": sorted(emails),
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n证据已保存 -> {OUT / 'exposure_detail.json'}")
sys.exit(0)
