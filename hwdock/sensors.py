"""温度与传感器采集。

按可用性逐级回退，尽可能在无内核驱动的前提下拿到真实温度：
  1. LibreHardwareMonitor / OpenHardwareMonitor（若已运行，最准，含 CPU 封装温度）
  2. NVML（NVIDIA 显卡，官方驱动自带）
  3. ACPI 温区（Windows 性能计数器，需要管理员）
  4. 硬盘温度（IOCTL_STORAGE_QUERY_PROPERTY，需要管理员）
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass, field
from typing import Optional

from .wmi import Wmi

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@dataclass
class Temp:
    value: float
    label: str = ""
    source: str = ""


@dataclass
class Sensors:
    cpu: Optional[Temp] = None
    gpu: Optional[Temp] = None
    disk: Optional[Temp] = None
    board: Optional[Temp] = None
    gpu_util: Optional[float] = None
    gpu_mem_used: int = 0
    gpu_mem_total: int = 0
    gpu_name: str = ""
    sources: list = field(default_factory=list)
    ts: float = 0.0


# ------------------------------------------------------------------ NVML
class Nvml:
    def __init__(self):
        self._lib = None
        self._handles = []
        self.ok = False
        try:
            self._lib = ctypes.WinDLL("nvml.dll")
            self._lib.nvmlInit_v2()
            n = ctypes.c_uint(0)
            self._lib.nvmlDeviceGetCount_v2(ctypes.byref(n))
            for i in range(min(4, int(n.value))):
                h = ctypes.c_void_p()
                if self._lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) == 0:
                    self._handles.append(h)
            self.ok = bool(self._handles)
        except Exception:
            self.close()

    def read(self) -> dict:
        """返回首个 GPU 的温度/占用/显存/型号。"""
        out: dict = {}
        if not self.ok or not self._handles:
            return out
        try:
            h = self._handles[0]
            t = ctypes.c_uint(0)
            if self._lib.nvmlDeviceGetTemperature(h, 0, ctypes.byref(t)) == 0:
                out["temp"] = float(t.value)
            name = ctypes.create_string_buffer(96)
            if self._lib.nvmlDeviceGetName(h, name, 96) == 0:
                out["name"] = name.value.decode("utf-8", "ignore")
            util = (ctypes.c_uint * 2)()
            if self._lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(util)) == 0:
                out["util"] = float(util[0])
            mem = (ctypes.c_ulonglong * 3)()
            if self._lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem)) == 0:
                out["mem_total"] = int(mem[0])
                out["mem_used"] = int(mem[2])
        except Exception:
            pass
        return out

    def close(self) -> None:
        try:
            if self._lib:
                self._lib.nvmlShutdown()
        except Exception:
            pass
        self._lib = None
        self._handles = []
        self.ok = False


# ------------------------------------------------------------------ 硬盘温度
_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_STORAGE_DEVICE_TEMPERATURE_PROPERTY = 22
_FILE_SHARE_READ, _FILE_SHARE_WRITE, _OPEN_EXISTING = 1, 2, 3
_GENERIC_READ = 0x80000000


class _StoragePropertyQuery(ctypes.Structure):
    _fields_ = [
        ("PropertyId", ctypes.c_ulong),
        ("QueryType", ctypes.c_ulong),
        ("AdditionalParameters", ctypes.c_ubyte * 1),
    ]


def _query_drive_temp(index: int) -> Optional[float]:
    """读取单块物理硬盘的温度（摄氏）。虚拟盘/RAID 通常不支持。"""
    handle = None
    try:
        _k32.CreateFileW.restype = wt.HANDLE
        handle = _k32.CreateFileW(
            f"\\\\.\\PhysicalDrive{index}", _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING, 0, None,
        )
        if not handle or handle == wt.HANDLE(-1).value:
            return None
        query = _StoragePropertyQuery(_STORAGE_DEVICE_TEMPERATURE_PROPERTY, 0,
                                      (ctypes.c_ubyte * 1)(0))
        buf = ctypes.create_string_buffer(1024)
        ret = wt.DWORD(0)
        ok = _k32.DeviceIoControl(
            handle, _IOCTL_STORAGE_QUERY_PROPERTY,
            ctypes.byref(query), ctypes.sizeof(query),
            buf, ctypes.sizeof(buf), ctypes.byref(ret), None,
        )
        if not ok or ret.value < 24:
            return None
        count = wt.USHORT.from_buffer(buf, 16).value
        if count == 0:
            return None
        temp = ctypes.c_short.from_buffer(buf, 20).value  # 摄氏度
        if -40 <= temp <= 150:
            return float(temp)
    except Exception:
        return None
    finally:
        if handle and handle != wt.HANDLE(-1).value:
            _k32.CloseHandle(handle)
    return None


_disk_state = {"supported": None, "tried": 0.0}


def storage_temperatures() -> dict[int, float]:
    """读取物理硬盘温度。不支持的机器（虚拟盘/RAID）60 秒后才重试。"""
    now = time.time()
    if _disk_state["supported"] == [] and now - _disk_state["tried"] < 60:
        return {}
    out: dict[int, float] = {}
    for i in range(8):
        t = _query_drive_temp(i)
        if t is not None:
            out[i] = t
    _disk_state["supported"] = sorted(out.keys())
    _disk_state["tried"] = now
    return out


def storage_models() -> dict[int, str]:
    """物理盘型号（STORAGE_DEVICE_DESCRIPTOR），WMI 失败时兜底。"""
    out: dict[int, str] = {}
    for i in range(8):
        handle = None
        try:
            _k32.CreateFileW.restype = wt.HANDLE
            handle = _k32.CreateFileW(
                f"\\\\.\\PhysicalDrive{i}", 0,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING, 0, None,
            )
            if not handle or handle == wt.HANDLE(-1).value:
                continue
            query = _StoragePropertyQuery(0, 0, (ctypes.c_ubyte * 1)(0))
            buf = ctypes.create_string_buffer(1024)
            ret = wt.DWORD(0)
            if _k32.DeviceIoControl(
                handle, _IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query), ctypes.sizeof(query),
                buf, ctypes.sizeof(buf), ctypes.byref(ret), None,
            ):
                off_vendor = ctypes.c_ulong.from_buffer(buf, 12).value
                off_product = ctypes.c_ulong.from_buffer(buf, 16).value
                text = []
                for off in (off_vendor, off_product):
                    if off:
                        raw = ctypes.string_at(ctypes.addressof(buf) + off, 64)
                        text.append(raw.split(b"\x00")[0].decode("utf-8", "ignore").strip())
                name = " ".join(t for t in text if t)
                if name:
                    out[i] = name
        except Exception:
            pass
        finally:
            if handle and handle != wt.HANDLE(-1).value:
                _k32.CloseHandle(handle)
    return out


# ------------------------------------------------------------------ 软件传感器源
_LHM_NS = [r"ROOT\LibreHardwareMonitor", r"ROOT\OpenHardwareMonitor"]


def _read_lhm() -> Optional[dict]:
    for ns in _LHM_NS:
        w = Wmi(ns)
        if not w.ok:
            continue
        try:
            rows = w.query(
                "SELECT Name, Value, Identifier, SensorType, Parent FROM Sensor "
                "WHERE SensorType='Temperature'",
                ["Name", "Value", "Identifier", "SensorType", "Parent"],
            )
        finally:
            w.close()
        if not rows:
            continue
        cpu, gpu, disk, board = [], [], [], []
        for r in rows:
            ident = (r.get("Identifier") or "").lower()
            name = (r.get("Name") or "").strip()
            val = r.get("Value")
            if val is None:
                continue
            try:
                val = float(val)
            except Exception:
                continue
            if not (-30 <= val <= 150):
                continue
            item = (name, val)
            if any(k in ident for k in ("/intelcpu", "/amdcpu", "/cpu")):
                cpu.append(item)
            elif "gpu" in ident:
                gpu.append(item)
            elif any(k in ident for k in ("/nvme", "/hdd", "/ssd")):
                disk.append(item)
            else:
                board.append(item)

        def pick(items, prefer):
            if not items:
                return None
            for p in prefer:
                for name, val in items:
                    if p.lower() in name.lower():
                        return name, val
            return max(items, key=lambda kv: kv[1])

        out = {}
        c = pick(cpu, ("Package", "CPU Package", "Core Max", "Tctl"))
        if c:
            out["cpu"] = (c[1], f"CPU {c[0]}")
        g = pick(gpu, ("GPU Core", "GPU Temperature", "GPU"))
        if g:
            out["gpu"] = (g[1], f"GPU {g[0]}")
        d = pick(disk, ("Temperature", "Drive Temperature"))
        if d:
            out["disk"] = (d[1], d[0])
        b = pick(board, ("System", "Motherboard", "Chipset"))
        if b:
            out["board"] = (b[1], b[0])
        if out:
            out["source"] = "LibreHardwareMonitor" if "Libre" in ns else "OpenHardwareMonitor"
            return out
    return None


def _read_acpi() -> Optional[tuple[float, str]]:
    """ACPI 温区（需要管理员）。返回 (摄氏, 温区名)。"""
    w = Wmi()
    if not w.ok:
        return None
    try:
        rows = w.query(
            "SELECT Name, Temperature, HighPrecisionTemperature "
            "FROM Win32_PerfFormattedData_Counters_ThermalZoneInformation",
            ["Name", "Temperature", "HighPrecisionTemperature"],
        )
        best = None
        for r in rows:
            hp = r.get("HighPrecisionTemperature")
            t = r.get("Temperature")
            celsius = None
            if isinstance(hp, (int, float)) and hp > 1000:      # 开尔文 ×10
                celsius = hp / 10.0 - 273.15
            elif isinstance(t, (int, float)) and t > 100:        # 开尔文
                celsius = float(t) - 273.15
            if celsius is None or not (-30 <= celsius <= 150):
                continue
            if best is None or celsius > best[0]:
                best = (celsius, str(r.get("Name") or "ACPI"))
        if best:
            return best
        rows = w.query(
            "SELECT CurrentTemperature, InstanceName FROM MSAcpi_ThermalZoneTemperature",
            ["CurrentTemperature", "InstanceName"],
        )
        for r in rows:
            cur = r.get("CurrentTemperature")
            if isinstance(cur, (int, float)) and cur > 0:
                celsius = cur / 10.0 - 273.15
                if -30 <= celsius <= 150:
                    return (celsius, str(r.get("InstanceName") or "ACPI"))
    finally:
        w.close()
    return None


# ------------------------------------------------------------------ 采集器
class SensorHub:
    """后台线程：静态信息只需要一次，温度按 2 秒轮询。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._nvml: Optional[Nvml] = None
        self._acpi_ok = True
        self._disk_temp_ok = True
        self.sensors = Sensors()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="sensors", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._nvml:
            self._nvml.close()

    def read(self) -> Sensors:
        with self._lock:
            return self.sensors

    # -------------------------------------------------- 内部
    def _loop(self) -> None:
        self._nvml = Nvml()
        prev = Sensors()
        last_full = 0.0
        while not self._stop.is_set():
            now = time.time()
            full = now - last_full >= 5.0
            try:
                sens = self._collect(prev, full)
                if full:
                    last_full = now
                prev = sens
                with self._lock:
                    self.sensors = sens
            except Exception:
                pass
            self._stop.wait(2.0)

    def _collect(self, prev: Sensors, full: bool) -> Sensors:
        """full=True 时刷新 WMI/SMART 等较慢的源，否则只更新 NVML。"""
        import time as _t
        s = Sensors(ts=_t.time())
        s.cpu, s.gpu, s.disk, s.board = prev.cpu, prev.gpu, prev.disk, prev.board
        s.sources = list(prev.sources)
        s.gpu_name = prev.gpu_name

        if not full:
            if self._nvml is None:
                self._nvml = Nvml()
            if self._nvml.ok:
                data = self._nvml.read()
                if "name" in data:
                    s.gpu_name = data["name"]
                if "temp" in data:
                    s.gpu = Temp(data["temp"], "GPU 核心", "NVML")
                if "util" in data:
                    s.gpu_util = data["util"]
                if data.get("mem_total"):
                    s.gpu_mem_total = data["mem_total"]
                    s.gpu_mem_used = data.get("mem_used", 0)
            return s

        sources: list[str] = []

        lhm = _read_lhm()
        if lhm:
            sources.append(lhm["source"])
            if "cpu" in lhm:
                s.cpu = Temp(lhm["cpu"][0], lhm["cpu"][1], lhm["source"])
            if "gpu" in lhm:
                s.gpu = Temp(lhm["gpu"][0], lhm["gpu"][1], lhm["source"])
            if "disk" in lhm:
                s.disk = Temp(lhm["disk"][0], lhm["disk"][1], lhm["source"])
            if "board" in lhm:
                s.board = Temp(lhm["board"][0], lhm["board"][1], lhm["source"])

        if self._nvml is None:
            self._nvml = Nvml()
        if self._nvml.ok:
            data = self._nvml.read()
            if "name" in data:
                s.gpu_name = data["name"]
            if "temp" in data:
                if s.gpu is None:
                    s.gpu = Temp(data["temp"], "GPU 核心", "NVML")
                sources.append("NVML")
            if "util" in data:
                s.gpu_util = data["util"]
            if data.get("mem_total"):
                s.gpu_mem_total = data["mem_total"]
                s.gpu_mem_used = data.get("mem_used", 0)

        if self._acpi_ok:
            acpi = _read_acpi()
            if acpi:
                value, name = acpi
                if s.board is None:
                    s.board = Temp(value, "系统温区", "ACPI")
                if s.cpu is None:
                    # ACPI 温区不是核心温度，标记清楚来源
                    s.cpu = Temp(value, "温区温度", "ACPI")
                sources.append("ACPI")
            elif not is_admin():
                self._acpi_ok = False

        if self._disk_temp_ok:
            temps = storage_temperatures()
            if temps:
                if s.disk is None:
                    s.disk = Temp(max(temps.values()), "硬盘温度", "SMART")
                sources.append("SMART")
            elif not is_admin():
                self._disk_temp_ok = False

        # 去重且保持顺序
        seen = set()
        s.sources = [x for x in sources if not (x in seen or seen.add(x))]
        return s
