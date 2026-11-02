"""对授权目标做只读安全测试（仅 GET/HEAD）。

目标来自 .env 的 ALLOWED_TARGETS 白名单，与 Ben 工具层的校验一致。
不写入、不删除、不爆破、不越权登录，仅采集可公开读取的响应作为证据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

BASE = "http://117.72.199.2:3000"
TIMEOUT = 15
OUT = Path(__file__).resolve().parents[2] / "reports"

SECURITY_HEADERS = [
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "strict-transport-security",
    "referrer-policy",
    "permissions-policy",
    "x-xss-protection",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
]

# 只读探测面：版本/配置/资料/监控/目录/认证边界
ENDPOINTS = [
    ("/", "应用根"),
    ("/robots.txt", "robots 声明"),
    ("/rest/admin/application-version", "版本泄露"),
    ("/rest/admin/application-configuration", "运行时配置泄露"),
    ("/api/Products", "商品 API"),
    ("/api/Users", "用户 API（未授权应 401）"),
    ("/api/Feedbacks", "反馈 API"),
    ("/api/Quantitys", "库存 API"),
    ("/api/SecurityQuestions", "安全问题 API"),
    ("/metrics", "Prometheus 指标"),
    ("/ftp", "FTP 目录列表"),
    ("/rest/user/whoami", "会话自检（匿名）"),
    ("/.well-known/security.txt", "安全联系声明"),
    ("/main.js", "前端 Bundle"),
]


def main() -> int:
    s = requests.Session()
    s.headers["User-Agent"] = "BenPT-readonly-audit/1.0"
    results = []

    print(f"目标: {BASE}\n")

    r = s.get(BASE + "/", timeout=TIMEOUT)
    print("=== 安全响应头（根路径）===")
    hdr_report = {}
    for h in SECURITY_HEADERS:
        val = r.headers.get(h)
        hdr_report[h] = val
        print(f"  {'OK  ' if val else 'MISS'} {h}: {val or '—'}")

    print("\n=== 只读端点探测 ===")
    for path, label in ENDPOINTS:
        try:
            resp = s.get(BASE + path, timeout=TIMEOUT, allow_redirects=False)
            body = resp.text or ""
            snippet = " ".join(body[:160].split())
            entry = {
                "path": path,
                "label": label,
                "status": resp.status_code,
                "content_type": resp.headers.get("Content-Type", ""),
                "length": len(body),
                "snippet": snippet,
            }
            print(f"  [{resp.status_code:>3}] {path:<40} {label}  len={len(body)}")
            if resp.status_code == 200 and len(body) < 4000:
                print(f"        {snippet[:140]}")
            results.append(entry)
        except Exception as exc:
            print(f"  [ERR] {path:<40} {type(exc).__name__}: {exc}")
            results.append({"path": path, "label": label, "error": str(exc)})

    OUT.mkdir(exist_ok=True)
    (OUT / "readonly_recon.json").write_text(
        json.dumps({"target": BASE, "security_headers": hdr_report,
                    "endpoints": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n证据已保存 -> {OUT / 'readonly_recon.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
