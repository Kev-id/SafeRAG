"""GET /api/v1/monitor — 盒子资源监控（CPU/内存/进程/TPU）。"""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.services import monitor_service

router = APIRouter(prefix="/api/v1")


class CPUInfo(BaseModel):
    usage_percent: float | None = None


class MemoryInfo(BaseModel):
    total_mb: int | None = None
    used_mb: int | None = None
    available_mb: int | None = None
    percent: float | None = None


class ProcessInfo(BaseModel):
    name: str
    pid: int
    rss_mb: int | None = None


class TPUProcessInfo(BaseModel):
    pid: int
    name: str
    memory_mb: int


class TPUInfo(BaseModel):
    tpuid: int | None = None
    temperature_c: int | None = None
    tpu_util_percent: int | None = None
    clock_mhz_min: int | None = None
    clock_mhz_max: int | None = None
    clock_mhz_cur: int | None = None
    npu_memory_used_mb: int | None = None
    npu_memory_total_mb: int | None = None
    processes: list[TPUProcessInfo] | None = None


class MonitorResponse(BaseModel):
    cpu: CPUInfo | None = None
    memory: MemoryInfo | None = None
    processes: list[ProcessInfo] = []
    tpu: TPUInfo | None = None


@router.get("/monitor", response_model=MonitorResponse)
async def get_monitor():
    """盒子 CPU / 内存 / 关键进程 / TPU（bm-smi）实时读数。"""
    raw = monitor_service.get_monitor()
    return MonitorResponse(
        cpu=CPUInfo(**raw["cpu"]) if raw["cpu"] else None,
        memory=MemoryInfo(**raw["memory"]) if raw["memory"] else None,
        processes=[ProcessInfo(**p) for p in raw["processes"]],
        tpu=TPUInfo(**raw["tpu"]) if raw["tpu"] else None,
    )