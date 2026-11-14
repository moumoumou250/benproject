#!/usr/bin/env bash
# 构建 Go 版 Bubble Tea TUI，输出到 Ben/bin/ben-tui。
#
# 运行时解析顺序（Ben/interface/tui/runtime.py::binary_command）：
#   1. 仓库内 go.mod 存在且 go 可用 → `go run ./cmd/ben-tui`（开发态，直接用源码）
#   2. 否则回落到 Ben/bin/ben-tui（打包态）
# 因此本脚本用于「让打包态也带上最新二进制」，或给没有 Go 工具链的机器预编译。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TUI_DIR="$PROJECT_ROOT/Ben/interface/tui"
OUT_DIR="$PROJECT_ROOT/Ben/bin"
BIN_NAME="ben-tui"
[ "${OS:-}" = "Windows_NT" ] && BIN_NAME="ben-tui.exe"
case "$(uname -s 2>/dev/null || true)" in
  MINGW*|MSYS*|CYGWIN*) BIN_NAME="ben-tui.exe" ;;
esac

if ! command -v go >/dev/null 2>&1; then
  echo "错误：未找到 go。构建 TUI 需要 Go 1.24 或更新版本。" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
echo "构建 $BIN_NAME ..."
(
  cd "$TUI_DIR"
  CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$OUT_DIR/$BIN_NAME" ./cmd/ben-tui
)

echo "完成：$OUT_DIR/$BIN_NAME"
ls -lh "$OUT_DIR/$BIN_NAME" 2>/dev/null || true
