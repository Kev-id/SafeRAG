"""业务层 — 盒子资源监控（CPU / 内存 / TPU / 关键进程）。

数据来源：
  - CPU / 内存：读 Linux /proc（零第三方依赖，ARM 盒子稳）
  - TPU：调 bm-smi 命令行（Sophgo BM1688 自带工具）
  - 关键进程：后端用 os.getpid()、Qwen 引擎扫 /proc/*/cmdline 找 "server.py"

非 Linux（如本机 Windows 调试）→ 对应字段返回 None，不崩。
"""

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

_IS_LINUX = os.name == "posix"


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------
def _read_proc_stat_times() -> tuple[int, int] | None:
    """读 /proc/stat 第一行（cpu 合计），返回 (total_jiffies, idle_jiffies)。"""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        # parts[0] = 'cpu', 后 8 个是 user nice system idle iowait irq softirq steal
        fields = [int(x) for x in parts[1:9]]
        idle = fields[3] + fields[4]          # idle + iowait
        total = sum(fields)
        return total, idle
    except Exception:
        return None, None


def _cpu_usage_percent() -> float | None:
    """两次采样间隔 200ms 的 CPU 使用率。"""
    t1, i1 = _read_proc_stat_times()
    if t1 is None:
        return None
    import time
    time.sleep(0.2)
    t2, i2 = _read_proc_stat_times()
    if t2 is None:
        return None
    dt = t2 - t1
    if dt <= 0:
        return None
    busy = dt - (i2 - i1)
    return round(busy / dt * 100, 1)


# ---------------------------------------------------------------------------
# 内存
# ---------------------------------------------------------------------------
def _mem_info() -> dict | None:
    """读 /proc/meminfo 的 MemTotal / MemAvailable。"""
    try:
        with open("/proc/meminfo") as f:
            d = {}
            for line in f:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    d[k] = int(v.split()[0])  # kB
            if "MemTotal" not in d:
                return None
    except Exception:
        return None

    total_kb = d["MemTotal"]
    avail_kb = d.get("MemAvailable", 0)
    used_kb = total_kb - avail_kb
    return {
        "total_mb": round(total_kb / 1024),
        "used_mb": round(used_kb / 1024),
        "available_mb": round(avail_kb / 1024),
        "percent": round(used_kb / total_kb * 100, 1) if total_kb else 0,
    }


# ---------------------------------------------------------------------------
# 进程
# ---------------------------------------------------------------------------
def _proc_rss_mb(pid: int) -> int | None:
    """读 /proc/PID/statm 第 2 字段（RSS，页数）→ MB。"""
    try:
        with open(f"/proc/{pid}/statm") as f:
            rss_pages = f.read().split()[1]
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024 if _IS_LINUX else 4
        return round(int(rss_pages) * page_kb / 1024)
    except Exception:
        return None


def _find_server_pids() -> list[int]:
    """扫 /proc/*/cmdline 找命令行含 server.py 的进程（Qwen 推理引擎）。"""
    pids = []
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as f:
                    cmd = f.read().decode(errors="ignore")
            except Exception:
                continue
            if "server.py" in cmd:
                pids.append(int(name))
    except Exception:
        pass
    return pids


def _processes() -> list[dict]:
    """当前关键进程占用：后端 + Qwen 引擎进程。"""
    result = []
    backend_pid = os.getpid()
    result.append({
        "name": "backend",
        "pid": backend_pid,
        "rss_mb": _proc_rss_mb(backend_pid),
    })
    for pid in _find_server_pids():
        result.append({
            "name": "qwen-engine",
            "pid": pid,
            "rss_mb": _proc_rss_mb(pid),
        })
    return result


# ---------------------------------------------------------------------------
# TPU（bm-smi）
# ---------------------------------------------------------------------------
def _read_bmsmi() -> str | None:
    """调 bm-smi 取原始文本。"""
    try:
        out = subprocess.run(
            ["bm-smi"], capture_output=True, text=True, timeout=10
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def _tpu_info() -> dict | None:
    """解析 bm-smi 文本 → TPU 温度/使用率/NPU 内存/时钟/进程。"""
    text = _read_bmsmi()
    if not text:
        return None
    info: dict = {}
    # ---- 温度 / Tpu-Util：取卡片段表格行 ----
    # 样例: "| 0 BM1688-SOC   SOC  N/A | 0  N/A  39C  N/A  N/A  N/A  N/A  0% |"
    m = re.search(r"\|\s+(\d+)\s+\S+\s+\S+\s+\S+\s+\|\s+0\s+\S+\s+(\d+)C\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)%\s+\|", text)
    if m:
        info["tpuid"] = int(m.group(1))
        info["temperature_c"] = int(m.group(2))
        info["tpu_util_percent"] = int(m.group(3))
    # ---- 时钟 ----
    # 样例: "|  N/A  N/A  N/A  25M  1000M  N/A|  N/A  Active  900M  N/A  0MB/ 8232MB |"
    m = re.search(r"\|\s+N/A\s+N/A\s+N/A\s+(\d+)M\s+(\d+)M\s+N/A\|", text)
    if m:
        info["clock_mhz_min"] = int(m.group(1))
        info["clock_mhz_max"] = int(m.group(2))
    m = re.search(r"\|\s+N/A\s+Active\s+(\d+)M\s+", text)
    if m:
        info["clock_mhz_cur"] = int(m.group(1))
    # ---- NPU 内存：取最后一个 "0MB/ NNNNMB"（Npu-Usage 总量，倒序取最后一个匹配）----
    matches = list(re.finditer(r"(\d+)MB/\s*(\d+)MB", text))
    if matches:
        m = matches[-1]  # 最后一个 = Npu-Usage 行（总 Npu 内存）
        info["npu_memory_used_mb"] = int(m.group(1))
        info["npu_memory_total_mb"] = int(m.group(2))
    # ---- TPU 进程（Processes 段）：PID  name  Memory ────
    procs = []
    for m in re.finditer(r"\|\s+\d+\s+(\d+)\s+(.+?)\s+\|\s+(\d+)MB\s+\|", text):
        procs.append({
            "pid": int(m.group(1)),
            "name": m.group(2).strip(),
            "memory_mb": int(m.group(3)),
        })
    if procs:
        info["processes"] = procs
    return info or None


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------
def get_monitor() -> dict:
    """汇总 CPU / 内存 / 进程 / TPU 读数。非 Linux → 对应字段 None。"""
    if not _IS_LINUX:
        return {
            "cpu": None, "memory": None,
            "processes": [{"name": "backend", "pid": os.getpid(), "rss_mb": None}],
            "tpu": None,
        }
    return {
        "cpu": {"usage_percent": _cpu_usage_percent()},
        "memory": _mem_info(),
        "processes": _processes(),
        "tpu": _tpu_info(),
    }