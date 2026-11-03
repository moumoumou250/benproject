.PHONY: help install test lint format format-check tui-build tui-test tui-lint viewer docker clean smoke

# Windows 下虚拟环境可执行文件位于 Scripts/，其余平台在 bin/。
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python.exe
else
PY := .venv/bin/python
endif

TUI_DIR := Ben/interface/tui
TUI_BIN := Ben/bin/ben-tui$(if $(filter Windows_NT,$(OS)),.exe)

help:
	@echo "可用目标："
	@echo "  install      - 安装运行依赖（pip -r requirements.txt）"
	@echo "  test         - 运行 pytest"
	@echo "  smoke        - 核心模块导入+构造冒烟（不调用模型，0 token）"
	@echo "  lint         - ruff check"
	@echo "  format       - ruff format"
	@echo "  format-check - ruff format --check"
	@echo "  tui-build    - 构建 Go TUI 到 $(TUI_BIN)"
	@echo "  tui-test     - go test -race ./..."
	@echo "  tui-lint     - gofmt 检查 + go vet"
	@echo "  viewer       - 重新构建本地查看器前端（产物需提交）"
	@echo "  docker       - 构建执行器沙箱镜像（ben-sandbox:dev）"
	@echo "  clean        - 清理缓存与构建产物"

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest -q

smoke:
	$(PY) scripts/smoke_core.py

lint:
	$(PY) -m ruff check .

format:
	$(PY) -m ruff format .

format-check:
	$(PY) -m ruff format --check .

tui-build:
	mkdir -p Ben/bin
	cd $(TUI_DIR) && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o ../../../$(TUI_BIN) ./cmd/ben-tui

tui-test:
	cd $(TUI_DIR) && go test -race ./...

tui-lint:
	cd $(TUI_DIR) && test -z "$$(gofmt -l .)" && go vet ./...

viewer:
	cd Ben/interface/viewer/frontend && npm ci && npm run build
	@echo "查看器已构建到 Ben/interface/viewer/static/（请提交产物）。"

docker:
	./scripts/docker.sh

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf build/ dist/
