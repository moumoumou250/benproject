# Eval suites（覆盖完整性 P3）

正样本靶场的**已知漏洞清单**放这里，供 `ben eval` 对照一次 run 的检出率。

可与 [skill-verification-plan-v2.md](../skill-verification-plan-v2.md) 的正样本 profile 共用同一靶场：skill verify 验「这类知识能不能复现」，eval 验「整次扫描 recall / 未覆盖面」。

## 用法

```bash
python main.py eval --suite juice-shop-smoke --run _artifacts/runs/<run_name>
python main.py eval --suite docs/eval-suites/juice-shop-smoke.json --run ... --json
```

退出码：`0` 全中或清单为空；`2` 参数/文件错误；`3` 有 miss（便于 CI）。

## 清单字段

| 字段 | 含义 |
|---|---|
| `expected[].id` | 条目名 |
| `expected[].type` | 漏洞类（无 `match` 时做子串兜底） |
| `expected[].match` | 须全部出现在 `vulnerabilities.json` 文本里的子串 |
| `surfaces` | 可选；应出现在 coverage 产物中的路径子串 |

示例见同目录 `juice-shop-smoke.json`。维护纪律：靶场升级后先改清单，再谈调 skill / 管线。
