#!/usr/bin/env bash
# 启动 1~2 个 Qwen3.5 推理引擎进程（在盒子上跑）。
#
# 为什么需要多进程：一个引擎进程 = 一份 bmodel = 内部 threading.Lock 完全串行，
# 要并行推理只能多进程（多端口）。每个进程独立加载一份 bmodel、独立持锁。
#
# 用法：
#   1. 改下面 MODEL_PATH / CONFIG_PATH 为盒子上实际路径
#   2. bash scripts/start_engines.sh
#   3. 引擎1 必须起来；引擎2 起不来会自动回退为单引擎（不阻塞后端启动）
set -u

# ---------- 按盒子实际情况修改 ----------
MODEL_PATH="/data/models/qwen3.5-4b/qwen3.5-4b.bmodel"   # bmodel 路径
CONFIG_PATH="/data/models/qwen3.5-4b/config"              # processor config 目录
PORT1=8000
PORT2=8001
DEVID1=0
DEVID2=0    # 风险点：同 devid 多进程能否共用未验证。引擎2 起不来就改 DEVID2=1（或查 bmrt 可用设备）
LOG_DIR="$(cd "$(dirname "$0")" && pwd)/logs"
# ---------------------------------------

# server.py 相对脚本目录解析，脚本放在仓库 scripts/ 下
SERVER="$(cd "$(dirname "$0")/.." && pwd)/Qwen3_5/python_demo/server.py"

mkdir -p "$LOG_DIR"

start_engine() {
    local port=$1 devid=$2
    nohup python "$SERVER" -m "$MODEL_PATH" -c "$CONFIG_PATH" --port "$port" -d "$devid" \
        > "$LOG_DIR/engine_$port.log" 2>&1 &
    echo $!
}

# 等就绪：进程还活着 + /health 可达。模型加载较慢，最多等 120s。
# server.py 是模型加载完才起 uvicorn，所以 /health 通 = 引擎真正可用。
wait_ready() {
    local port=$1 pid=$2
    for _ in $(seq 1 60); do
        kill -0 "$pid" 2>/dev/null || { echo "引擎进程已退出（模型加载失败？），见 $LOG_DIR/engine_$port.log"; return 1; }
        if curl -sf "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

echo "启动引擎1（端口 $PORT1, devid $DEVID1）..."
pid1=$(start_engine "$PORT1" "$DEVID1")
if wait_ready "$PORT1" "$pid1"; then
    echo "引擎1 OK (pid=$pid1)  http://127.0.0.1:$PORT1"
else
    echo "引擎1 启动失败，终止脚本。请检查 MODEL_PATH / CONFIG_PATH / DEVID1。"
    exit 1
fi

echo "启动引擎2（端口 $PORT2, devid $DEVID2）..."
pid2=$(start_engine "$PORT2" "$DEVID2")
if wait_ready "$PORT2" "$pid2"; then
    echo "引擎2 OK (pid=$pid2)  http://127.0.0.1:$PORT2"
else
    echo "引擎2 启动失败（多半是同 devid 多进程受限），回退为单引擎，仅用 :$PORT1。"
    echo "修复提示：把 DEVID2 改成空闲设备号；或后端只配 QWEN_BASE_URLS=http://127.0.0.1:8000。"
    kill -0 "$pid2" 2>/dev/null && kill "$pid2" 2>/dev/null
fi

echo "引擎就绪。日志：$LOG_DIR/engine_*.log"
