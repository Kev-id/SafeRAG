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
# 「路灯」：任何一步失败立刻打印 行号+命令+退出码，绝不无声自杀
trap 's=$?; echo "!! make_deb 在行 $LINENO 失败: $BASH_COMMAND (exit $s)" >&2; exit $s' ERR

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT/../.." && pwd)"
VERSION="1.0.0"
ARCH="${ARCH:-arm64}"
MODELS_DIR=""
FRONTEND=""
WHEELS=""
MAKE_WHEELS=""
OFFLINE_APT=""
OUT_DIR=""

usage() {
  cat <<EOF
用法: make_deb.sh [选项]
  --frontend DIR   前端静态目录        (默认: /opt/emergency-platform/frontend; 否则 ../emergency-platform/frontend, /data2/www/...)
  --models DIR     模型目录            (默认: 盒子上优先 /data2/models(真含 bmodel)，否则仓库根 models/。两种布局都认: 平铺 或 Qwen3_5/ 分组)
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

# ---- 默认模型来源：优先盒子 /data2/models（真含 4B bmodel 时），否则仓库根 models/ ----
if [ -z "$MODELS_DIR" ]; then
  if [ -f /data2/models/Qwen3_5/qwen3.5-4b_w4bf16_bm1688.bmodel ] \
     || [ -f /data2/models/qwen3.5-4b_w4bf16_bm1688.bmodel ]; then
    MODELS_DIR="/data2/models"
  else
    MODELS_DIR="$ROOT/models"
  fi
fi

command -v dpkg-deb >/dev/null || { echo "需要 dpkg-deb（请在 Debian 环境运行）"; exit 1; }

# ---- 参数探测/校验 ----
[ -d "$MODELS_DIR" ] || { echo "缺模型目录: $MODELS_DIR"; exit 1; }
if [ -z "$FRONTEND" ]; then
  for cand in /opt/emergency-platform/frontend \
              "$ROOT/../emergency-platform/frontend" \
              /data2/www/emergency-platform/frontend; do
    [ -d "$cand" ] && FRONTEND="$cand" && break
  done
fi
[ -n "$FRONTEND" ] && [ -d "$FRONTEND" ] || { echo "未找到前端目录(--frontend)"; exit 1; }

# ---- 解析模型来源（同时支持两种目录结构：仓库根平铺 models/ 或 盒子 /data2/models 的 Qwen3_5/ 分组）----
BM_4B=""; for c in "$MODELS_DIR/Qwen3_5/qwen3.5-4b_w4bf16_bm1688.bmodel" "$MODELS_DIR/qwen3.5-4b_w4bf16_bm1688.bmodel"; do [ -f "$c" ] && BM_4B="$c" && break; done
[ -n "$BM_4B" ] || { echo "缺 4B bmodel: $MODELS_DIR 下需有 qwen3.5-4b_w4bf16_bm1688.bmodel（顶层或 Qwen3_5/ 内）"; exit 1; }
BM_2B=""; for c in \
  "$MODELS_DIR/Qwen3_5/qwen3.5-2b-int4-autoround_w4bf16_seq8192_bm1688_2core_history_dynamic_20260728_111707.bmodel" \
  "$MODELS_DIR/qwen3.5-2b-int4-autoround_w4bf16_seq8192_bm1688_2core_history_dynamic_20260728_111707.bmodel"; do
  [ -f "$c" ] && BM_2B="$c" && break
done
[ -n "$BM_2B" ] || { echo "缺 2B bmodel: ...111707.bmodel 未找到（顶层或 Qwen3_5/ 内）"; exit 1; }

# 引擎 config：优先 --models/Qwen3_5/config（盒子实况），否则仓库 Qwen3_5/config
CFG_SRC=""
[ -d "$MODELS_DIR/Qwen3_5/config" ] && CFG_SRC="$MODELS_DIR/Qwen3_5/config"
[ -z "$CFG_SRC" ] && [ -d "$ROOT/Qwen3_5/config" ] && CFG_SRC="$ROOT/Qwen3_5/config"
[ -n "$CFG_SRC" ] || { echo "缺引擎 config（/data2/models/Qwen3_5/config 或仓库 Qwen3_5/config）"; exit 1; }

# embedding：优先知名目录名 bge-small-zh-*；否则扫非 reranker 的 bge-* 目录
EMB_SRC=""
for c in "$MODELS_DIR/bge-small-zh-v1.5" "$MODELS_DIR/bge-small-zh-onnx" "$MODELS_DIR/bge-small-zh"; do
  [ -f "$c/onnx/model_quantized.onnx" ] && [ -f "$c/tokenizer.json" ] && EMB_SRC="$c" && break
done
if [ -z "$EMB_SRC" ]; then
  for d in "$MODELS_DIR"/bge-*/; do
    case "$d" in *reranker*) continue;; esac
    [ -f "$d/onnx/model_quantized.onnx" ] && [ -f "$d/tokenizer.json" ] && EMB_SRC="$d" && break
  done
fi
[ -n "$EMB_SRC" ] || { echo "缺 embedding 模型（需 onnx/model_quantized.onnx + tokenizer.json）"; exit 1; }

# reranker：bge-reranker-base 目录（含 onnx/model_quantized.onnx）
RERK_SRC=""
for d in "$MODELS_DIR/bge-reranker-base" "$MODELS_DIR"/*reranker*; do
  [ -d "$d" ] && [ -f "$d/onnx/model_quantized.onnx" ] && RERK_SRC="$d" && break
done
[ -n "$RERK_SRC" ] || { echo "缺 reranker 模型（bge-reranker-base）"; exit 1; }
echo "== 模型来源: $MODELS_DIR  (embedding=$EMB_SRC reranker=$RERK_SRC)"

if [ -n "$MAKE_WHEELS" ]; then
  [ "$(uname -m)" = "aarch64" ] || { echo "--make-wheels 必须在本机为 aarch64 时使用(x86 会抓到错轮子); 请用 --wheels 传已在盒子生成的目录"; exit 1; }
fi
[ -n "$WHEELS" ] && [ -d "$WHEELS" ] || { [ -z "$WHEELS" ] || { echo "wheels 目录不存在: $WHEELS"; exit 1; }; }

[ -z "$OUT_DIR" ] && OUT_DIR="$(mktemp -d)/deb"
mkdir -p "$OUT_DIR"

# 关键：dpkg-deb 的临时 .deb 默认落在 TMPDIR(/tmp→根分区, 常不够 6G)
# 必须指到大盘，否则报 "No space left on device"（本次就翻在这）
export TMPDIR="$OUT_DIR"
[ -n "${DEBUG:-}" ] && echo "== TMPDIR → $TMPDIR"

# 空间预检：staging(约=模型+代码) + 输出的 deb 双份，粗估需 ≥ 15G
avail_kb=$(df -k "$OUT_DIR" | awk 'END{print $4}')
if [ "$avail_kb" -lt $((15 * 1024 * 1024)) ]; then
  echo "⚠ 输出盘 $OUT_DIR 剩余 $(echo "$avail_kb 1024" | awk '{printf "%.1fG", $1/$2/1024}')，打包峰值约需 15G；"
  echo "  建议 -o /data2/... （根部空间小，模型+deb 会压爆）"
fi

echo "== 源码: $ROOT  |  版本: $VERSION  |  前端: $FRONTEND"

# ===========================================================================
# 打 deb：一律用 dpkg-deb（标准写法，保证 dpkg -i 能装）。
# 手拼 ar+tar 虽可有进度，但 tar 格式与 dpkg 严格解析不兼容 → 装不上(P2C)
# ===========================================================================
build_deb() {
  local pkg="$1" out="$2" label="$3"
  echo "-- $label: dpkg-deb 打包（$(du -sh "$pkg" | cut -f1)，静默，大包请耐心数分钟）..."
  dpkg-deb -Znone --build "$pkg" "$out" >/dev/null
  dpkg-deb --info "$out" >/dev/null 2>&1 || { echo "!! $out 不是合法 deb"; return 1; }
  echo "   ✅ $out  $(du -h "$out" | cut -f1)"
}

# ===========================================================================
# ① models 包 → /data2/models
# ===========================================================================
P1="$OUT_DIR/_models"
mkdir -p "$P1/DEBIAN" "$P1/data2/models"
cp "$SCRIPT/debian/models/control" "$P1/DEBIAN/control"
sed -i "s/@VERSION@/$VERSION/g; s/@ARCH@/$ARCH/g" "$P1/DEBIAN/control"
[ "$(tail -c 1 "$P1/DEBIAN/control")" = "$(printf '\n')" ] || printf '\n' >> "$P1/DEBIAN/control"

echo "== 组装 models 包（进度见逐文件输出） ..."
rsync -a --progress "$EMB_SRC/."   "$P1/data2/models/bge-small-zh-v1.5/" 2>&1 | tr '\r' '\n' | tail -1
rsync -a --progress "$RERK_SRC/."  "$P1/data2/models/bge-reranker-base/" 2>&1 | tr '\r' '\n' | tail -1
mkdir -p "$P1/data2/models/Qwen3_5"
echo "-- 复制 4B bmodel ($(du -h "$BM_4B" | cut -f1))"
rsync -a --progress "$BM_4B" "$P1/data2/models/Qwen3_5/"
echo "-- 复制 2B bmodel ($(du -h "$BM_2B" | cut -f1))"
rsync -a --progress "$BM_2B" "$P1/data2/models/Qwen3_5/"
rsync -a "$CFG_SRC/."   "$P1/data2/models/Qwen3_5/config/"
MDEB="$OUT_DIR/saferag-models_${VERSION}_${ARCH}.deb"
build_deb "$P1" "$MDEB" "models 包"

# ===========================================================================
# ② app 包 → /data/SafeRAG(代码) + /opt/emergency-platform/frontend(前端) + /etc(nginx/systemd) + /opt/saferag(wheels/offline-apt)
# ===========================================================================
P2="$OUT_DIR/_app"
mkdir -p "$P2/DEBIAN" "$P2/data/SafeRAG" "$P2/opt/emergency-platform" \
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

# 前端 → /opt/emergency-platform/frontend（nginx site root 即此路径）
rsync -a "$FRONTEND/" "$P2/opt/emergency-platform/frontend/"

# nginx 站点配置 → /etc/nginx（只带 SafeRAG 自己的站点文件）
# ⚠ 绝不能整树铺：scripts/deploy/nginx 曾是目标机 /etc/nginx 的整树快照，里面的
#   fastcgi.conf、mime.types、koi-utf、snippets/*、modules-enabled/*、
#   sites-available/default、nginx.conf 等都是 nginx-common/nginx 包自带的库存文件。
#   整树打进 deb 后, 在已装过 nginx 的盒子上 dpkg 会拒绝接管这些"别人的文件" →
#   trying to overwrite '/etc/nginx/fastcgi.conf', which is also in package nginx-common
mkdir -p "$P2/etc/nginx/sites-available" "$P2/etc/nginx/sites-enabled"
cp -f "$SCRIPT/nginx/sites-available/SafeRAG" "$P2/etc/nginx/sites-available/"
cp -f "$SCRIPT/nginx/sites-enabled/SafeRAG"   "$P2/etc/nginx/sites-enabled/"

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
  echo "== --make-wheels: 下载依赖树 → wheels/ ..."
  python3 -m pip download -r "$ROOT/backend/requirements.txt" -d "$P2/opt/saferag/wheels"
  # sdist(纯 Python 老包如 jieba 常常只发 tar.gz) 现场构建成 wheel，离线装才不出岔子
  for s in "$P2/opt/saferag/wheels"/*.tar.gz; do
    [ -e "$s" ] || continue
    echo "   sdist→wheel(约几十秒): $(basename "$s")"
    # --no-build-isolation：用系统 setuptools/jieba 老包所需仅此，避免离线再去抓隔离构建依赖
    python3 -m pip wheel --no-deps --no-build-isolation --wheel-dir "$P2/opt/saferag/wheels" "$s" \
      && rm -f "$s" \
      || { echo "   ⚠ 构建失败，留在包内(离线可能也要编译)"; }
  done
  # find 而非 ls 通配符：无匹配时 find 返回 0，ls *.tar.gz 会以退出码 2 触发 set -e/pipefail 误杀
  n_bad=$(find "$P2/opt/saferag/wheels" -maxdepth 1 -name '*.tar.gz' 2>/dev/null | wc -l)
  if [ "$n_bad" != 0 ]; then
    echo "!! 仍有 $n_bad 个 sdist(未能 wheel 化)，离线装会失败或需编译:"
    ls "$P2/opt/saferag/wheels"/*.tar.gz 2>/dev/null
    exit 1
  fi
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
[ "$(tail -c 1 "$P2/DEBIAN/control")" = "$(printf '\n')" ] || printf '\n' >> "$P2/DEBIAN/control"
cp "$SCRIPT/debian/app/postinst"  "$P2/DEBIAN/postinst"
cp "$SCRIPT/debian/app/prerm"     "$P2/DEBIAN/prerm"
cp "$SCRIPT/debian/app/conffiles" "$P2/DEBIAN/conffiles"
chmod 755 "$P2/DEBIAN/postinst" "$P2/DEBIAN/prerm"

ADEB="$OUT_DIR/saferag_${VERSION}_${ARCH}.deb"
build_deb "$P2" "$ADEB" "app 包"

echo
echo "== ✅ 打包完成，输出目录: $OUT_DIR"
echo "安装(盒子上, 全离线):"
echo "   dpkg -i saferag-models_${VERSION}_${ARCH}.deb"
echo "   dpkg -i saferag_${VERSION}_${ARCH}.deb"
ls -lh "$OUT_DIR"/*.deb