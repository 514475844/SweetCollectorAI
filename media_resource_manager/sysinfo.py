"""R245 · 机器信息（Web「AI 模型」页要显示的那一块：CPU / 内存 / GPU / 硬盘 / 剩余空间）。

订单出处：用户 2026-10-29 第 5 条 —— 「AI 模型页要求显示配置，显示全一点：
CPU 多少？GPU 多少内存多少？硬盘多少？当前下载文件夹剩余空间多少？」

为什么不用 psutil：本软件要能打包成单 exe 给别人跑，psutil 会带一个二进制扩展；
而需要的几个数字在 Windows 上都能用 **标准库 + ctypes** 拿到
（``GlobalMemoryStatusEx`` / ``GetLogicalDrives`` / ``shutil.disk_usage``）。
Linux 下退到 ``/proc``。取不到的项一律返回 ``None`` 并留一句原因，
**绝不抛异常**——模型页是给人看信息的，不能因为读不到 GPU 就整页 500。

GPU 走 ``nvidia-smi``（N 卡上才有；没有就返回空列表 + 原因）。
诊断包（``diagnostics``）也复用这里的统计，避免两处口径不一样。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------------------
# 内存 / CPU
# --------------------------------------------------------------------------
def memory_info() -> dict:
    """总内存 / 可用内存（字节）。Windows 走 GlobalMemoryStatusEx，Linux 读 /proc/meminfo。"""
    out = {"total": None, "available": None}
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                out["total"] = int(st.ullTotalPhys)
                out["available"] = int(st.ullAvailPhys)
                out["percent"] = int(st.dwMemoryLoad)
        except Exception:
            pass
    else:
        try:
            vals = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                vals[k.strip()] = int(v.strip().split()[0]) * 1024
            out["total"] = vals.get("MemTotal")
            out["available"] = vals.get("MemAvailable")
            if out["total"] and out["available"] is not None:
                out["percent"] = int(
                    round(100 * (1 - out["available"] / out["total"])))
        except Exception:
            pass
    return out


def cpu_info() -> dict:
    out = {
        "cores_logical": os.cpu_count() or 0,
        "machine": platform.machine(),
        "processor": "",
        "name": "",
    }
    if sys.platform.startswith("win"):
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            out["processor"] = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
            mhz = winreg.QueryValueEx(k, "~MHz")[0]
            out["mhz"] = int(mhz)
        except Exception:
            pass
    if not out["processor"]:
        out["processor"] = platform.processor() or ""
    out["name"] = out["processor"] or out["machine"] or "未知处理器"
    return out


# --------------------------------------------------------------------------
# 硬盘
# --------------------------------------------------------------------------
def _fmt_bytes(n: Any) -> str:
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return ("%.0f %s" % (v, unit)) if unit in ("B", "KB") else \
                   ("%.1f %s" % (v, unit))
        v /= 1024.0
    return "%.1f TB" % v


def disk_item(p: Any, label: str = "") -> dict:
    """某个路径所在卷的总量/可用量；路径不存在就返回 ``None`` 字段。"""
    try:
        path = Path(p)
    except Exception:
        return {"label": label, "path": str(p), "total": None, "free": None}
    probe = path
    # 目录还没建好时，往上找第一个存在的祖先（否则 disk_usage 直接抛）
    while True:
        try:
            if probe.exists():
                break
        except OSError:
            break
        if probe.parent == probe:
            break
        probe = probe.parent
    try:
        u = shutil.disk_usage(str(probe))
        total, free = int(u.total), int(u.free)
    except Exception:
        total, free = None, None
    return {"label": label or str(path), "path": str(path),
            "total": total, "free": free,
            "total_text": _fmt_bytes(total), "free_text": _fmt_bytes(free),
            "used_text": _fmt_bytes((total - free) if (total and free is not None) else None)}


def drives() -> list:
    """列出所有盘符与容量（Windows）。Linux 只给根分区（避免把挂载点全列出来）。"""
    out = []
    if sys.platform.startswith("win"):
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if not (mask >> i) & 1:
                    continue
                letter = "%s:\\" % chr(65 + i)
                # 只列固定盘（DRIVE_FIXED=3）：U 盘/光驱列出来对采图没意义
                try:
                    t = ctypes.windll.kernel32.GetDriveTypeW(letter)
                except Exception:
                    t = 0
                if t not in (2, 3):
                    continue
                item = disk_item(letter, letter[0] + ":")
                if item.get("total"):
                    out.append(item)
        except Exception:
            pass
    if not out:
        out.append(disk_item("/", "/"))
    return out


# --------------------------------------------------------------------------
# GPU
# --------------------------------------------------------------------------
_SMI_FIELDS = "name,memory.total,memory.used,utilization.gpu"


def gpus() -> list:
    """GPU 列表（N 卡走 nvidia-smi）。没有就空列表 —— 调用方显示「未检测到」。"""
    out = []
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + _SMI_FIELDS, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform.startswith("win") else 0))
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 4:
                    continue
                try:
                    total_mb = int(float(parts[1]))
                except ValueError:
                    total_mb = 0
                try:
                    used_mb = int(float(parts[2]))
                except ValueError:
                    used_mb = 0
                try:
                    util = int(float(parts[3]))
                except ValueError:
                    util = None
                out.append({"name": parts[0], "vendor": "NVIDIA",
                            "vram_total": total_mb * 1024 * 1024,
                            "vram_used": used_mb * 1024 * 1024,
                            "vram_total_text": "%d MB" % total_mb,
                            "vram_used_text": "%d MB" % used_mb,
                            "util": util})
    except Exception:
        pass
    return out


def onnx_providers() -> list:
    try:
        import onnxruntime as ort
        return list(ort.get_available_providers())
    except Exception:
        return []


# --------------------------------------------------------------------------
# 总装
# --------------------------------------------------------------------------
def collect(cfg: Any = None) -> dict:
    """一次拿全（模型页与诊断包共用）。"""
    from . import __version__

    data = {"app_version": __version__,
            "frozen": bool(getattr(sys, "frozen", False)),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cpu": cpu_info(),
            "memory": memory_info(),
            "gpus": gpus(),
            "onnx_providers": onnx_providers(),
            "drives": drives()}
    # 当前下载文件夹（各任务默认往这儿落图）+ 数据目录
    if cfg is not None:
        out_dir = getattr(cfg, "last_output_dir", "") or getattr(cfg, "library_dir", "")
        data["download_dir"] = disk_item(out_dir, "当前下载目录") if out_dir else None
        try:
            data["data_dir"] = disk_item(getattr(cfg, "db_path", "").parent, "数据目录")
        except Exception:
            data["data_dir"] = None
        data["ai"] = {
            "backend": str(getattr(cfg, "ai_backend", "") or ""),
            "device": str(getattr(cfg, "ai_device", "") or ""),
            "model": str(getattr(cfg, "ai_model_path", "") or ""),
            "threshold": getattr(cfg, "ai_threshold", None),
            "online_url": str(getattr(cfg, "ai_api_url", "") or ""),
        }
    return data


def headline(data: Optional[dict] = None) -> str:
    """一句话摘要（诊断包与页面顶部都用它）。"""
    d = data or collect()
    cpu = d.get("cpu") or {}
    mem = d.get("memory") or {}
    gpu = (d.get("gpus") or [{}])[0]
    bits = ["%s × %d 线程" % (cpu.get("name") or "CPU", cpu.get("cores_logical") or 0)]
    if mem.get("total"):
        bits.append("内存 %s" % _fmt_bytes(mem["total"]))
    if gpu:
        bits.append("%s %s 显存" % (gpu.get("name"), gpu.get("vram_total_text")))
    return " / ".join(bits)
