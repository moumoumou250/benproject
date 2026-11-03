#!/usr/bin/env bash
# 构建执行器沙箱镜像（containers/Dockerfile）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE="${BEN_SANDBOX_IMAGE:-ben-sandbox}"
TAG="${1:-dev}"

echo "构建 $IMAGE:$TAG ..."
docker build \
  -f "$PROJECT_ROOT/containers/Dockerfile" \
  -t "$IMAGE:$TAG" \
  "$PROJECT_ROOT"

echo "完成：$IMAGE:$TAG"
echo "在 .env 里用 BEN_IMAGE=$IMAGE:$TAG 指定它。"
