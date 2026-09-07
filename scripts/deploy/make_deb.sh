#!/usr/bin/env bash
# =============================================================================
# SafeRAG .deb 打包脚本（在 Debian 环境运行：盒子本身或任一 Debian 构建机）
#
# 产出：一个自包含 deb，装了它 = 依赖自动装 + 三个 systemd 服务 + nginx 全就位。
#
# 用法:
#   bash scripts/deploy/make_deb.sh \
#       --backend ../SafeRAG --infer ../saferag-infer --frontend ../前端/dist \
#       --version 1.2.0 [--make-wheels] [--out /tmp]
#
# 说明:
#   - 模型 bmodel/ONNX 不装、只引用（/data2/models…），省 5GB 包体；
#   - 离线依赖: 给 --make-wheels 会现场 pip download(在 aarch64 Debian 上最准)，
#     或 --wheels <目录> 直接塞进包；
#   - postinst 自动: pip 装依赖 → 生成 api.env → 起 3 服务 → nginx reload。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$(cd "$SCRIPT_DIR/../.." && pwd)"
INFER=""
# 前端是"静态 html 文件夹"(非构建产物)直接可部署；默认取同级 emergency-platform/frontend
FRONTEND="${FRONTEND:-$(cd "$BACKEND/../emergency-platform" && pwd)/frontend}"
VERSION="1.0.0"
WHEELS=""
MAKE_WHEELS=""
OUT_DIR="/tmp"
DEB_NAME="saferag"

usage() {
  cat <<'EOF'
用法: make_deb.sh [选项]
  --backend DIR    后端源码目录    (默认 当前仓库)
  --infer DIR      引擎源码目录    (必填: saferag-infer)
  --frontend DIR   前端静态目录    (默认 ../emergency-platform/frontend；无需构建，静态即部署)
  --version X.Y.Z  deb 版本        (默认 1.0.0)
  --wheels DIR     离线依赖目录(预生成, 塞进包)
  --make-wheels    现场 pip download 生成 aarch64 wheels(需网络)
  --name NAME      包名            (默认 saferag)
  -o, --out DIR    输出目录        (默认 /tmp)
  -h, --help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --backend) BACKEND="$(cd "$2" && pwd)"; shift 2 ;;
    --infer) INFER="$(cd "$2" && pwd)"; shift 2 ;;
    --frontend) FRONTEND="$(cd "$2" && pwd)"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --wheels) WHEELS="$(cd "$2" && pwd)"; shift 2 ;;
    --make-wheels) MAKE_WHEELS=1; shift ;;
    --name) DEB_NAME="$2"; shift 2 ;;
    -o|--out) OUT_DIR="$(cd "$2" && pwd)"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
done

command -v dpkg-deb >/dev/null || { echo "需要 dpkg-deb（在 Debian 环境运行本脚本）"; exit 1; }
[ -n "$INFER" ] || { echo "--infer 必填"; exit 1; }
if [ ! -d "$FRONTEND" ]; then
  echo "找不到前端目录: $FRONTEND（用 --frontend 指定你的静态 html 文件夹）" >&2; exit 1
fi

STAGE="$(mktemp -d)/root"
mkdir -p "$STAGE/DEBIAN"

# ---------- 控制文件 ----------
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $DEB_NAME
Version: $VERSION
Section: misc
Priority: optional
Architecture: arm64
Depends: python3, python3-pip, nginx
Maintainer: SafeRAG <dev@saferag.local>
Description: SafeRAG 安全生产 AI 报告生成服务(后端+推理引擎+前端)
EOF

cp "$SCRIPT_DIR/debian/postinst" "$STAGE/DEBIAN/postinst"
cp "$SCRIPT_DIR/debian/prerm"    "$STAGE/DEBIAN/prerm"
cp "$SCRIPT_DIR/debian/conffiles" "$STAGE/DEBIAN/conffiles"
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

# ---------- 内容 ----------
mkdir -p "$STAGE/opt/saferag/frontend" "$STAGE/opt/saferag/wheels"
mkdir -p "$STAGE/data/saferag" "$STAGE/data/saferag-infer"
mkdir -p "$STAGE/etc/systemd/system"   "$STAGE/etc/nginx/conf.d" "$STAGE/etc/saferag"

if command -v rsync >/dev/null; then
  rsync -a --exclude 'data' --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'release' --exclude 'eval_results' "$BACKEND/" "$STAGE/data/saferag/app/"
  rsync -a --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' "$INFER/" "$STAGE/data/saferag-infer/"
else
  cp -r "$BACKEND/." "$STAGE/data/saferag/app/"
  cp -r "$INFER/." "$STAGE/data/saferag-infer/"
fi
cp -r "$FRONTEND/." "$STAGE/opt/saferag/frontend/"

# ---------- 离线依赖 ----------
if [ -n "$WHEELS" ] && ls "$WHEELS"/*.whl >/dev/null 2>&1; then
  cp "$WHEELS"/*.whl "$STAGE/opt/saferag/wheels/"
fi
if [ -n "$MAKE_WHEELS" ]; then
  echo "== 生成 aarch64 wheels ..."
  python3 -m pip download \
    -r "$BACKEND/backend/requirements.txt" -r "$INFER/requirements.txt" \
    -d "$STAGE/opt/saferag/wheels"
fi

# ---------- systemd / nginx / 占位 env ----------
cp "$SCRIPT_DIR/saferag-api.service"      "$STAGE/etc/systemd/system/"
cp "$SCRIPT_DIR/saferag-qwen4b.service"   "$STAGE/etc/systemd/system/"
cp "$SCRIPT_DIR/saferag-qwen2b.service"   "$STAGE/etc/systemd/system/"
cp "$SCRIPT_DIR/nginx-saferag.conf"       "$STAGE/etc/nginx/conf.d/saferag.conf"
echo "# postinst 首次安装时生成(含随机 JWT); 升级保留" \
     > "$STAGE/etc/saferag/api.env"

# ---------- 构建 ----------
mkdir -p "$OUT_DIR"
DEB="$OUT_DIR/${DEB_NAME}_${VERSION}_arm64.deb"
dpkg-deb --build "$STAGE" "$DEB" >/dev/null
rm -rf "$(dirname "$STAGE")"
echo "== ✅ 生成 $DEB"
du -sh "$DEB"
echo "== 安装(在目标盒子上):  sudo apt install ./$(basename "$DEB")"