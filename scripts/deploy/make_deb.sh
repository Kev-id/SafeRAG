#!/usr/bin/env bash
# =============================================================================
# SafeRAG 离线 deb 打包 —— 一次产出两个 deb：
#   saferag-models_<V>_<ARCH>.deb   模型(≈6G)：装到 /data2/models
#   saferag_<V>_<ARCH>.deb          代码+前端+wheels+offline-apt+nginx+systemd
#
# 装机(全离线，两条):
#   dpkg -i saferag-models_<V>_arm64.deb
#   dpkg -i saferag_<V>_arm64.deb        # 自动装依赖/起服务/nginx(缺 nginx 时用 offline-apt/)
#
# 用法(在 Debian 环境运行; 建议在目标盒子或 aarch64 Debian 容器):
#   bash scripts/deploy/make_deb.sh \
#       --wheels wheelhouse/ --offline-apt offline-apt/ --version 1.0.0
#   或: --make-wheels  现场生成 arm64 wheels(需网络, 且本机必须是 aarch64)
# =============================================================================
set -euo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT/../.." && pwd)"
VERSION="1.0.0"
ARCH="${ARCH:-arm64}"
MODELS_DIR="$ROOT/models"
FRONTEND=""
WHEELS=""
MAKE_WHEELS=""
OFFLINE_APT=""
OUT_DIR=""

usage() {
  cat <<EOF
用法: make_deb.sh [选项]
  --frontend DIR   前端静态目录        (默认: ../emergency-platform/frontend; 否则 /data2/www/emergency-platform/frontend)
  --models DIR     模型目录            (默认: 仓库根 models/)
  --wheels DIR     离线 Python 轮子目录(推荐, 已备好则塞进包)
  --make-wheels    现场 pip download 生成 wheels(需网络且本机为 aarch64)
  --offline-apt DIR nginx/依赖 的 .deb 目录(目标机无 nginx 时离线补装)
  --version X.Y.Z 版本号              (默认 1.0.0)
  -o, --out DIR   输出目录            (默认: 系统临时目录)
  -h, --help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --frontend) FRONTEND="$2"; shift 2 ;;
    --models) MODELS_DIR="$2"; shift 2 ;;
    --wheels) WHEELS="$2"; shift 2 ;;
    --make-wheels) MAKE_WHEELS=1; shift ;;
    --offline-apt) OFFLINE_APT="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    -o|--out) OUT_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 2 ;;
  esac
done

command -v dpkg-deb >/dev/null || { echo "需要 dpkg-deb（请在 Debian 环境运行）"; exit 1; }

# ---- 参数探测/校验 ----
[ -d "$MODELS_DIR" ] || { echo "缺模型目录: $MODELS_DIR"; exit 1; }
if [ -z "$FRONTEND" ]; then
  for cand in "$ROOT/../emergency-platform/frontend" /data2/www/emergency-platform/frontend; do
    [ -d "$cand" ] && FRONTEND="$cand" && break
  done
fi
[ -n "$FRONTEND" ] && [ -d "$FRONTEND" ] || { echo "未找到前端目录(--frontend)"; exit 1; }

# bmodel 必在
for f in qwen3.5-4b_w4bf16_bm1688.bmodel \
         qwen3.5-2b-int4-autoround_w4bf16_seq8192_bm1688_2core_history_dynamic_20260728_111707.bmodel; do
  [ -f "$MODELS_DIR/$f" ] || { echo "缺 $MODELS_DIR/$f"; exit 1; }
done
[ -d "$ROOT/Qwen3_5/config" ] || { echo "缺仓库 Qwen3_5/config(引擎 config)"; exit 1; }

if [ -n "$MAKE_WHEELS" ]; then
  [ "$(uname -m)" = "aarch64" ] || { echo "--make-wheels 必须在本机为 aarch64 时使用(x86 会抓到错轮子); 请用 --wheels 传已在盒子生成的目录"; exit 1; }
fi
[ -n "$WHEELS" ] && [ -d "$WHEELS" ] || { [ -z "$WHEELS" ] || { echo "wheels 目录不存在: $WHEELS"; exit 1; }; }

[ -z "$OUT_DIR" ] && OUT_DIR="$(mktemp -d)/deb"
mkdir -p "$OUT_DIR"
echo "== 源码: $ROOT  |  版本: $VERSION  |  前端: $FRONTEND"

# ===========================================================================
# ① models 包 → /data2/models
# ===========================================================================
P1="$OUT_DIR/_models"
mkdir -p "$P1/DEBIAN" "$P1/data2/models"
cp "$SCRIPT/debian/models/control" "$P1/DEBIAN/control"
sed -i "s/@VERSION@/$VERSION/g; s/@ARCH@/$ARCH/g" "$P1/DEBIAN/control"

echo "== 组装 models 包 ..."
rsync -a "$MODELS_DIR/bge-small-zh-onnx/"  "$P1/data2/models/bge-small-zh-v1.5/"
rsync -a "$MODELS_DIR/bge-reranker-base/"  "$P1/data2/models/bge-reranker-base/"
mkdir -p "$P1/data2/models/Qwen3_5"
cp -v "$MODELS_DIR/qwen3.5-4b_w4bf16_bm1688.bmodel"          "$P1/data2/models/Qwen3_5/" >/dev/null
cp -v "$MODELS_DIR/qwen3.5-2b-int4-autoround_w4bf16_seq8192_bm1688_2core_history_dynamic_20260728_111707.bmodel" "$P1/data2/models/Qwen3_5/" >/dev/null
rsync -a "$ROOT/Qwen3_5/config/"  "$P1/data2/models/Qwen3_5/config/"
MDEB="$OUT_DIR/saferag-models_${VERSION}_${ARCH}.deb"
dpkg-deb --build "$P1" "$MDEB" >/dev/null
echo "   ✅ $MDEB  $(du -h "$MDEB" | cut -f1)"

# ===========================================================================
# ② app 包 → /data/SafeRAG(代码) + /data2/www/...(前端) + /etc(nginx/systemd) + /opt/saferag(wheels/offline-apt)
# ===========================================================================
P2="$OUT_DIR/_app"
mkdir -p "$P2/DEBIAN" "$P2/data/SafeRAG" "$P2/data2/www/emergency-platform" \
         "$P2/etc/nginx" "$P2/etc/systemd/system" "$P2/opt/saferag"

echo "== 组装 app 包 ..."
# 代码 → /data/SafeRAG（排除 .git/模型/运行数据/构建中间件）
if command -v rsync >/dev/null; then
  rsync -a --exclude '.git' --exclude 'models' --exclude 'backend/data' \
        --exclude 'eval_results' --exclude 'scripts/deploy/build' \
        --exclude '__pycache__' --exclude '*.pyc' --exclude '*.bmodel' \
        "$ROOT/" "$P2/data/SafeRAG/"
else
  echo "!! 无 rsync，代码将含运行时数据，建议先装 rsync"; exit 1
fi

# 前端 → /data2/www/emergency-platform/frontend（nginx site root 即此路径）
rsync -a "$FRONTEND/" "$P2/data2/www/emergency-platform/frontend/"

# nginx 配置整树 → /etc/nginx
rsync -a "$SCRIPT/nginx/" "$P2/etc/nginx/"

# systemd 服务 → /etc/systemd/system
cp "$SCRIPT/systemd/"*.service "$P2/etc/systemd/system/"

# wheels（离线 Python 依赖）
if [ -n "$WHEELS" ]; then
  mkdir -p "$P2/opt/saferag/wheels"
  rsync -a "$WHEELS/" "$P2/opt/saferag/wheels/"
  echo "   wheels: $(ls "$P2/opt/saferag/wheels" | wc -l) 个 .whl"
fi
if [ -n "$MAKE_WHEELS" ]; then
  mkdir -p "$P2/opt/saferag/wheels"
  python3 -m pip download -r "$ROOT/backend/requirements.txt" -d "$P2/opt/saferag/wheels"
  n_bad=$(ls "$P2/opt/saferag/wheels"/*.tar.gz 2>/dev/null | wc -l)
  [ "$n_bad" = 0 ] || echo "!! 有 $n_bad 个源码包(sdist)——离线装不了，需手工处理"
fi

# offline-apt（nginx/依赖 的 .deb，目标机没 nginx 时离线补装）
if [ -n "$OFFLINE_APT" ] && [ -d "$OFFLINE_APT" ]; then
  mkdir -p "$P2/opt/saferag/offline-apt"
  rsync -a "$OFFLINE_APT/" "$P2/opt/saferag/offline-apt/"
  echo "   offline-apt: $(ls "$P2/opt/saferag/offline-apt"/*.deb 2>/dev/null | wc -l) 个 .deb"
fi

# debian 脚本
cp "$SCRIPT/debian/app/control" "$P2/DEBIAN/control"
sed -i "s/@VERSION@/$VERSION/g; s/@ARCH@/$ARCH/g" "$P2/DEBIAN/control"
cp "$SCRIPT/debian/app/postinst"  "$P2/DEBIAN/postinst"
cp "$SCRIPT/debian/app/prerm"     "$P2/DEBIAN/prerm"
cp "$SCRIPT/debian/app/conffiles" "$P2/DEBIAN/conffiles"
chmod 755 "$P2/DEBIAN/postinst" "$P2/DEBIAN/prerm"

ADEB="$OUT_DIR/saferag_${VERSION}_${ARCH}.deb"
dpkg-deb --build "$P2" "$ADEB" >/dev/null
echo "   ✅ $ADEB  $(du -h "$ADEB" | cut -f1)"

echo
echo "== ✅ 打包完成，输出目录: $OUT_DIR"
echo "安装(盒子上, 全离线):"
echo "   dpkg -i saferag-models_${VERSION}_${ARCH}.deb"
echo "   dpkg -i saferag_${VERSION}_${ARCH}.deb"
ls -lh "$OUT_DIR"/*.deb