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
    """两次采样间隔 100ms 的 CPU 使用率。"""
    t1, i1 = _read_proc_stat_times()
    if t1 is None:
        return None
    import time
    time.sleep(0.1)
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
    """扫 /proc/*/cmdline 精确找 Qwen 推理引擎进程。

    匹配 Qwen3_5/python_demo/server.py——避免把 led_server.py、
    python -m http.server 等同样含 "server.py" 的进程误当引擎。
    """
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
            if "Qwen3_5/python_demo/server.py" in cmd:
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
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"   # CSI 序列（\x1b[1;1H、\x1b[37m 等）
    r"|\x1b[78]"                 # 保存/恢复光标（\x1b7 \x1b8）
    r"|\x1b[=<>(A-Z]"            # 其它 ESC 指令（\x1b= \x1b> \x1b( 等）
)


def _strip_ansi(text: str) -> str:
    """剥掉 bm-smi 的 ANSI 终端转义序列，得到纯文本表格。

    bm-smi 在非交互终端（subprocess）下会输出大量光标定位/切屏转义
    （\x1b[1;1H、\x1b[?47h、\x1b7\x1b8 等），打散表格顺序。剥掉后
    只剩可见字符，数值才能被正则匹配。
    """
    return _ANSI_RE.sub("", text)


def _run_bmsmi(args: list[str]) -> str | None:
    """跑一次 bm-smi（-noloop 强制退出 + TERM=dumb 关 ANSI），返回剥好的纯文本。"""
    try:
        out = subprocess.run(
            ["/opt/sophon/libsophon-current/bin/bm-smi", "-noloop", *args],
            capture_output=True, text=True, timeout=2,
            env={**os.environ, "TERM": "dumb"},
        )
        if out.returncode != 0:
            return None
        return _strip_ansi(out.stdout)
    except Exception:
        return None


def _read_bmsmi() -> str | None:
    """跑两次 bm-smi 合并：主表（温度/时钟/NPU 内存）+ core_util（每核利用率）。

    bm-smi 加 -core_util 会改变输出结构、丢掉主表；分开跑两次各自解析最稳：
      - ["-"]                 → 温度/时钟/NPU
      - ["-core_util"]        → core 利用率（无 TTY 下只有它给 %）
    用空行分隔拼回，正则各自命中不串扰。
    """
    main = _run_bmsmi([])
    util = _run_bmsmi(["-core_util"])
    if main is None and util is None:
        return None
    return "\n\n".join(s for s in (main, util) if s)


def _tpu_info() -> dict | None:
    """解析 bm-smi 文本 → TPU 温度/使用率/NPU 内存/时钟。

    用容错式匹配（不依赖表格列对齐）：bm-smi 的 \r 样式表格在终端会被
    wrap 成一整行，规范化列位置的正则会错位。改为按关键词取：
      温度 = 第一个 "NN C"（chipT）；利用率 = "NN%"（Tpu-Util）；
      时钟对 = "NNM NNM"（min/max）；当前时钟 = "Active NNM"；
      NPU 内存 = "xMB/ yMB" 里总量最大那对（取 Npu-Usage 的 8192，
      而非 Ion-Usage 的 8232）。
    """
    text = _read_bmsmi()
    if not text:
        return None
    info: dict = {}
    # 温度：第一个 NN C（chipT）
    m = re.search(r"(\d+)C", text)
    if m:
        info["temperature_c"] = int(m.group(1))
    # 利用率：core_id util 表里取所有核 util，聚合（如均值）作为 tpu_util_percent。
    # bm-smi -core_util 输出形如 "0 0% 1 0%"（每核一行）。若一个 % 都没有则 None。
    utils = re.findall(r"(\d+)%", text)
    if utils:
        info["tpu_util_percent"] = round(sum(int(u) for u in utils) / len(utils), 1)
    # 时钟 min/max：第一个 "NNM NNM"
    m = re.search(r"(\d+)M\s+(\d+)M", text)
    if m:
        info["clock_mhz_min"] = int(m.group(1))
        info["clock_mhz_max"] = int(m.group(2))
    # 当前时钟：Active NNM
    m = re.search(r"Active\s+(\d+)M", text)
    if m:
        info["clock_mhz_cur"] = int(m.group(1))
    # NPU 内存：所有 "xMB/ y" 对里取总量最大的一对。
    # 注：bm-smi 同时有 Ion-Usage 和 Npu-Usage 两套（total 40 / 8192 / 8232 各异），
    # 整段找 "xMB/ y"（y 可不带 MB 后缀，因 ANSI 剥离后列粘连丢后缀），
    # 取 total 最大的一对作为内存总量。
    pairs = [(int(a), int(b)) for a, b in re.findall(r"(\d+)MB/\s*(\d+)", text)]
    if pairs:
        used, total = max(pairs, key=lambda p: p[1])
        info["npu_memory_used_mb"] = used
        info["npu_memory_total_mb"] = total
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