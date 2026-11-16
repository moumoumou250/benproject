#!/usr/bin/env bash
# BenProject 本地安装：创建/复用 .venv，安装依赖，注册 `ben` 到 PATH。
#
# 用法：
#   ./install.sh                 # 装依赖 + editable + ~/.local/bin/ben
#   ./install.sh --deps-only     # 只装 requirements.txt
#   ./install.sh --link-only     # 不装包，只把已有 .venv/bin/ben 链到 PATH
#   source .venv/bin/activate && ben --help
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DEPS_ONLY=0
LINK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --deps-only) DEPS_ONLY=1 ;;
    --link-only) LINK_ONLY=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $arg（支持 --deps-only / --link-only / --help）" >&2
      exit 2
      ;;
  esac
done

pick_python() {
  local c
  for c in python3.12 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

link_ben_to_path() {
  local ben_bin="$1"
  local dest_dir="${HOME}/.local/bin"
  local dest="${dest_dir}/ben"

  mkdir -p "$dest_dir"
  ln -sfn "$ben_bin" "$dest"
  echo "[*] 已链接：$dest -> $ben_bin"

  case ":${PATH}:" in
    *":${dest_dir}:"*) ;;
    *)
      echo
      echo "[!] ${dest_dir} 不在 PATH。把下面一行加进 ~/.zshrc（或 ~/.bashrc）后重新打开终端："
      echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
      echo
      echo "    当前会话可立即生效："
      echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
      ;;
  esac
}

if ! PYTHON="$(pick_python)"; then
  echo "[FAIL] 未找到 python3（需要 >= 3.12）" >&2
  exit 1
fi

VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
MAJOR="${VER%%.*}"
MINOR="${VER#*.}"
if (( MAJOR < 3 || (MAJOR == 3 && MINOR < 12) )); then
  echo "[FAIL] 需要 Python >= 3.12，当前是 $VER（$PYTHON）" >&2
  exit 1
fi

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
BEN="$VENV/bin/ben"

# uv/损坏的 venv 可能有 activate 却没有可用 python
if [[ ! -x "$PY" ]]; then
  echo "[*] 创建虚拟环境：$VENV"
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV"
else
  echo "[*] 复用虚拟环境：$VENV"
fi

if (( LINK_ONLY == 1 )); then
  if [[ ! -x "$BEN" ]]; then
    echo "[FAIL] 没有 $BEN。请先跑完整安装：./install.sh" >&2
    exit 1
  fi
  link_ben_to_path "$BEN"
  echo "[OK] 仅完成 PATH 链接。试一下：ben vuln --list"
  exit 0
fi

echo "[*] 升级 pip（经 python -m pip，兼容无 pip 脚本的 uv venv）"
"$PY" -m pip install -U pip setuptools wheel

if [[ ! -f "$ROOT/requirements.txt" ]]; then
  echo "[FAIL] 缺少 requirements.txt" >&2
  exit 1
fi

echo "[*] 安装 requirements.txt"
"$PY" -m pip install -r "$ROOT/requirements.txt"

if (( DEPS_ONLY == 0 )); then
  echo "[*] 可编辑安装本仓库（注册 console script: ben）"
  # editable 不触发 Go TUI 编译钩子（见 scripts/tui_sidecar_hook.py）
  "$PY" -m pip install -e "$ROOT"
fi

if [[ ! -x "$BEN" ]]; then
  echo "[FAIL] 安装后仍没有 $BEN" >&2
  exit 1
fi

echo "[*] 校验：$BEN vuln --list"
"$BEN" vuln --list >/dev/null

link_ben_to_path "$BEN"

echo
echo "[OK] 安装完成。"
echo "    激活 venv：  source \"$VENV/bin/activate\""
echo "    全局命令：  ben --help"
echo "    模块方式：  \"$PY\" -m Ben.interface.main --help"
