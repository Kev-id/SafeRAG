#!/usr/bin/env bash
# =============================================================================
# SafeRAG 一键部署(自包含, 不依赖 docker-compose)
# 在「部署文件夹」里运行: models/  nginx/  emergency-platform/frontend/  +  saferag-images.tar.gz
#
# 用法: cd 文件夹 && sudo bash install.sh
# 兼容:
#   - docker 19.03 / 20.10 都能跑(裸 docker run, 不需要 compose v2)
#   - 1688 / 1684x 通用: 自动探测 bmodel(4B 做文档, 有 2B 则对话用它, 否则同用 4B)
#     + 只透传实际存在的 /dev/bm* 节点(节点随板卡而异)
# 幂等: 先清同名旧容器再重建; 重复执行安全。
# =============================================================================
set -euo pipefail
trap 's=$?; echo "!! install 在行 $LINENO 失败: $BASH_COMMAND (exit $s)" >&2; exit $s' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

[ "$(id -u)" -eq 0 ] || { echo "!! 需 root: sudo bash $0"; exit 1; }
command -v docker >/dev/null || { echo "!! 未装 docker"; exit 1; }

echo "== 1/6 预检 =="
[ -f saferag-images.tar.gz ] || { echo "!! 缺 saferag-images.tar.gz"; exit 1; }
[ -d models/Qwen3_5 ] || { echo "!! 缺 models/Qwen3_5"; exit 1; }
BM4B="$(ls models/Qwen3_5/*4b*.bmodel 2>/dev/null | head -1 || true)"
BM2B="$(ls models/Qwen3_5/*2b*.bmodel 2>/dev/null | head -1 || true)"
[ -n "$BM4B" ] || { echo "!! models/Qwen3_5 里找不到 4B bmodel(named *4b*.bmodel)"; exit 1; }
[ -f emergency-platform/frontend/index.html ] || echo "  ⚠ 缺前端 index.html(nginx 会 404)"
[ -f nginx/saferag.conf ] || { echo "!! 缺 nginx/saferag.conf"; exit 1; }
[ -d "$SCRIPT_DIR/models/bge-small-zh-v1.5" ] || echo "  ⚠ models/ 里 embedding 目录应为 bge-small-zh-v1.5"
[ -d /opt/sophon/libsophon-current/lib ] || { echo "!! 缺 /opt/sophon/libsophon-current/lib(NPU 运行时)"; exit 1; }

# NPU 设备节点: 只透传实际存在的(1688: bm-tpu0+bmdev-ctl+ion; 1684x 一般 bmdev-ctl+ion, 有的也有 bm-tpu0)
DEV=""
for d in /dev/bm-tpu0 /dev/bmdev-ctl /dev/ion; do [ -e "$d" ] && DEV="$DEV --device $d:$d"; done
[ -n "$DEV" ] || echo "  !! 没有 /dev/bm* 设备节点, 引擎无法跑"
echo "  设备:${DEV:- 无}  4B:$(basename "$BM4B")  2B:${BM2B:+$(basename "$BM2B")}(有则对话用)  无:对话复用4B"

echo "== 2/6 载入镜像 =="
docker load -i saferag-images.tar.gz
if ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -qx 'nginx:alpine'; then
  echo "!! tar 里没有 nginx:alpine(少了镜像), 请重打全三镜像的 tar"; exit 1
fi
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'saferag|nginx' || true

echo "== 3/6 清旧容器(幂等) =="
for n in qwen-doc qwen-chat saferag-backend saferag-nginx; do
  docker rm -f "$n" >/dev/null 2>&1 || true
done

echo "== 4/6 起引擎(4B=文档; 对话优先 2B, 无则同用 4B) =="
LIBS="/opt/sophon/libsophon-current/lib"
CHAT_BM="${BM2B:-$BM4B}"
[ "$CHAT_BM" = "$BM4B" ] && CHAT_LABEL="tpu-qwen3.5-4B" || CHAT_LABEL="tpu-qwen3.5-2B"

docker run -d --name qwen-doc --network host --restart unless-stopped $DEV \
  --shm-size 2g -e LD_LIBRARY_PATH="$LIBS" \
  -v "$LIBS:$LIBS:ro" -v "$SCRIPT_DIR/models:/models" \
  saferag-qwen:1.0.0 python server.py -m "/models/Qwen3_5/$(basename "$BM4B")" \
  -c /models/Qwen3_5/config --host 127.0.0.1 --port 8000

docker run -d --name qwen-chat --network host --restart unless-stopped $DEV \
  --shm-size 2g -e LD_LIBRARY_PATH="$LIBS" \
  -v "$LIBS:$LIBS:ro" -v "$SCRIPT_DIR/models:/models" \
  saferag-qwen:1.0.0 python server.py -m "/models/Qwen3_5/$(basename "$CHAT_BM")" \
  -c /models/Qwen3_5/config --host 127.0.0.1 --port 8001

echo "== 5/6 起后端 + nginx =="
if ss -lntp 2>/dev/null | grep -qE ':80 '; then
  echo "  ⚠ 80 端口被占用(常见: 板卡自带 nginx)。请先: sudo systemctl stop nginx(或停占用者)再重跑"
else
  echo "  80 端口空闲"
fi
mkdir -p /data/SafeRAG/backend/data
docker run -d --name saferag-backend --network host --restart unless-stopped \
  -e JWT_SECRET="${JWT_SECRET:-saferag-dev-secret-please-change-in-prod}" \
  -e QWEN_CONNECT_TIMEOUT=5 -e QWEN_READ_TIMEOUT=600 -e QWEN_MAX_TOKENS=4096 \
  -e "QWEN_CHAT_MODEL=$CHAT_LABEL" \
  -v "$SCRIPT_DIR/models:/models:ro" \
  -v /data/SafeRAG/backend/data:/data/saferag/backend/data \
  saferag-backend:1.0.0

docker run -d --name saferag-nginx --network host --restart unless-stopped \
  -v "$SCRIPT_DIR/emergency-platform/frontend:/usr/share/nginx/html:ro" \
  -v "$SCRIPT_DIR/nginx/saferag.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine

sleep 20
docker ps --format '{{.Names}}\t{{.Status}}'

echo "== 6/6 验证(引擎就绪需 ~1 分钟) =="
h4=""; h2=""
for _ in $(seq 1 36); do
  h4="$(curl -s -m 3 http://127.0.0.1:8000/health 2>/dev/null || true)"
  h2="$(curl -s -m 3 http://127.0.0.1:8001/health 2>/dev/null || true)"
  [ -n "$h4" ] && [ -n "$h2" ] && break
  sleep 5
done
echo "  4B: ${h4:-未就绪}   2B/对话: ${h2:-未就绪}"
echo "  前端: $(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1/ 2>/dev/null || echo 超时)"
echo "  docs: $(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1/docs 2>/dev/null || echo 超时)"
echo
echo "== ✅ 完成 =="
echo "  前端 http://<盒子IP>/   API 文档 http://<盒子IP>/docs   引擎状态: bm-smi"
echo "  引擎日志: docker logs qwen-doc / qwen-chat"