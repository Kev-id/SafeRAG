#!/usr/bin/env bash
# =============================================================================
# SafeRAG Docker 镜像构建 —— 在 aarch64 环境跑(本环境就是 BM1688 盒子)。
# 产物: saferag-qwen / saferag-backend 两个镜像, 都可 docker save/load 搬到同架构另一台。
#
# 用法:  bash scripts/deploy/docker/build_images.sh [版本]     # 默认 1.0.0
#
# 说明:
#   - 引擎镜像不烤 libsophon(NPU 运行时): 运行时由盒子把 /opt/sophon/libsophon-current/lib
#     挂进容器并设 LD_LIBRARY_PATH(见 docker-compose.yml), 与每台盒子的驱动版本配对。
#     因此引擎镜像无需为了驱动版本在新盒重建, 和 backend 一样可搬。
#   - 构建上下文临时组装, 严禁把 /data2/models(17G) 或仓库根带进(会让 build 卡死/拷死)。
#   - 盒子有网(docker hub + pypi 走代理实测可达), 基镜像与依赖都现场拉。
# =============================================================================
set -euo pipefail
trap 's=$?; echo "!! build_images 在行 $LINENO 失败: $BASH_COMMAND (exit $s)" >&2; exit $s' ERR

VERSION="${1:-1.0.0}"
DOCKER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 盒子上默认取当前部署目录; 需要别处打包时用 SAFERAG_ROOT 覆盖
ROOT="${SAFERAG_ROOT:-/data/SafeRAG}"

[ "$(uname -m)" = "aarch64" ] || { echo "!! 必须在 aarch64 环境构建"; exit 1; }
[ -d "$ROOT/Qwen3_5/python_demo" ] || { echo "!! 缺 $ROOT/Qwen3_5/python_demo"; exit 1; }
[ -d "$ROOT/backend" ]             || { echo "!! 缺 $ROOT/backend"; exit 1; }

echo "== 1/2 构建引擎镜像 saferag-qwen:$VERSION (上下文仅 python_demo) ..."
CTXE="$(mktemp -d)"
cp -a "$ROOT/Qwen3_5/python_demo" "$CTXE/python_demo"
docker build --network host -f "$DOCKER_DIR/Dockerfile.engine" -t "saferag-qwen:$VERSION" "$CTXE"

echo "== 2/2 构建后端镜像 saferag-backend:$VERSION ..."
CTXB="$(mktemp -d)"
cp -a "$ROOT/backend"                                   "$CTXB/backend"
cp "$DOCKER_DIR/requirements-backend.txt"               "$CTXB/requirements-backend.txt"
docker build --network host -f "$DOCKER_DIR/Dockerfile.backend" -t "saferag-backend:$VERSION" "$CTXB"

echo
echo "== ✅ 构建完成:"
docker images | grep -E 'saferag-(qwen|backend)' || true
echo
echo "下一步(切到容器跑):"
echo "  1) systemctl stop qwen qwen_chat saferag nginx      # 见 README.md 切换一节"
echo "  2) cd scripts/deploy/docker && docker compose up -d"
echo "回滚: docker compose down && systemctl enable --now qwen qwen_chat saferag nginx"