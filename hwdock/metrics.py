"""硬件指标采集：psutil（CPU/内存/磁盘/网络） + PDH（GPU、磁盘忙碌度）。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import psutil

HISTORY_LEN = 90


# ------------------------------------------------------------------ PDH
class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [
        ("szName", wt.LPWSTR),
        ("FmtValue", _PDH_FMT_COUNTERVALUE),
    ]


PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
ERROR_SUCCESS = 0


class PdhQueryError(Exception):
    pass


_pdh = ctypes.WinDLL("pdh.dll")


class PdhArrayCounter:
    """读取形如 "\\GPU Engine(*)\\Utilization Percentage" 的多实例计数器。

    group 决定聚合方式：
      "engtype_max" -> 按引擎类型分别求和后取最大值（近似任务管理器 GPU 占用）
      "sum"         -> 全部实例求和
      其它          -> 取最大值
    """

    def __init__(self, path: str, group: str = "sum"):
        self.path = path
        self.group = group
        self.available = False
        self._query = wt.HANDLE()
        self._counter = wt.HANDLE()
        self._last = 0.0
        try:
            q = wt.HANDLE()
            if _pdh.PdhOpenQueryW(None, None, ctypes.byref(q)) != 0:
                return
            c = wt.HANDLE()
            status = _pdh.PdhAddEnglishCounterW(q, path, None, ctypes.byref(c))
            if status != 0:
                _pdh.PdhCloseQuery(q)
                return
            _pdh.PdhCollectQueryData(q)
            self._query, self._counter = q, c
            self.available = True
        except Exception:
            self.available = False

    def read(self) -> Optional[float]:
        if not self.available:
            return None
        try:
            if _pdh.PdhCollectQueryData(self._query) != 0:
                return None
            buf = self._collect_array()
            if buf is None:
                return None
            values = self._aggregate(buf)
            if values is None:
                return self._last
            self._last = values
            return values
        except Exception:
            return self._last

    # -------------------------------------------------- 内部
    def _collect_array(self):
        size = wt.DWORD(0)
        count = wt.DWORD(0)
        status = _pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE, ctypes.byref(size),
            ctypes.byref(count), None,
        )
        # PDH 返回 LONG（有符号），需按无符号比较状态码
        if (status & 0xFFFFFFFF) != PDH_MORE_DATA or size.value == 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        status = _pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE, ctypes.byref(size),
            ctypes.byref(count), buf,
        )
        if (status & 0xFFFFFFFF) != 0:
            return None
        return buf, int(count.value)

    def _aggregate(self, packed):
        buf, count = packed
        item = _PDH_FMT_COUNTERVALUE_ITEM_W
        item_size = ctypes.sizeof(item)
        base = ctypes.addressof(buf)
        by_type: dict[str, float] = {}
        total = 0.0
        peak = 0.0
        found = False
        for i in range(count):
            it = item.from_address(base + i * item_size)
            if it.FmtValue.CStatus not in (0, 1):
                continue
            val = float(it.FmtValue.doubleValue)
            if val < 0 or val > 1000:
                continue
            found = True
            name = it.szName or ""
            total += val
            peak = max(peak, val)
            if "engtype_" in name:
                eng = name.rsplit("engtype_", 1)[-1]
                by_type[eng] = by_type.get(eng, 0.0) + val
        if not found:
            return None
        if self.group == "engtype_max":
            if not by_type:
                return min(100.0, total)
            return min(100.0, max(by_type.values()))
        if self.group == "sum":
            return min(100.0, total)
        return min(100.0, peak)

    def close(self) -> None:
        try:
            if self._query:
                _pdh.PdhCloseQuery(self._query)
        except Exception:
            pass
        self.available = False


_pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wt.HANDLE)]
_pdh.PdhOpenQueryW.restype = wt.LONG
_pdh.PdhAddEnglishCounterW.argtypes = [
    wt.HANDLE, wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wt.HANDLE)
]
_pdh.PdhAddEnglishCounterW.restype = wt.LONG
_pdh.PdhCollectQueryData.argtypes = [wt.HANDLE]
_pdh.PdhCollectQueryData.restype = wt.LONG
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.DWORD),
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
]
_pdh.PdhGetFormattedCounterArrayW.restype = wt.LONG
_pdh.PdhCloseQuery.argtypes = [wt.HANDLE]
_pdh.PdhCloseQuery.restype = wt.LONG


# ------------------------------------------------------------------ 快照
@dataclass
class Snapshot:
    cpu: float = 0.0
    cpu_freq: float = 0.0
    cores: int = 0
    threads: int = 0
    mem_pct: float = 0.0
    mem_used: int = 0
    mem_total: int = 1
    gpu: Optional[float] = None
    disk_pct: float = 0.0
    disk_read: float = 0.0
    disk_write: float = 0.0
    net_down: float = 0.0
    net_up: float = 0.0
    gpu_name: str = ""
    ts: float = 0.0


@dataclass
class History:
    cpu: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))
    mem: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))
    gpu: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))
    disk: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))
    net_rx: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))
    net_tx: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))

    def push(self, snap: Snapshot) -> None:
        self.cpu.append(snap.cpu)
        self.mem.append(snap.mem_pct)
        self.gpu.append(snap.gpu if snap.gpu is not None else 0.0)
        self.disk.append(snap.disk_read + snap.disk_write)
        self.net_rx.append(snap.net_down)
        self.net_tx.append(snap.net_up)


class Sampler:
    """后台采样线程，避免阻塞 UI。"""

    def __init__(self, interval: float = 1.0):
        self.interval = max(0.4, interval)
        self.snapshot = Snapshot()
        self.history = History()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gpu_counter: Optional[PdhArrayCounter] = None
        self._disk_idle: Optional[PdhArrayCounter] = None
        self._gpu_name = ""

    # -------------------------------------------------- 生命周期
    def start(self) -> None:
        self._init_counters()
        self._thread = threading.Thread(target=self._loop, name="sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for c in (self._gpu_counter, self._disk_idle):
            if c:
                c.close()

    def _init_counters(self) -> None:
        self._gpu_counter = PdhArrayCounter(
            r"\GPU Engine(*)\Utilization Percentage", group="engtype_max"
        )
        self._disk_idle = PdhArrayCounter(
            r"\PhysicalDisk(_Total)\% Idle Time", group="first"
        )
        self._gpu_name = gpu_display_name()

    # -------------------------------------------------- 采样
    def _loop(self) -> None:
        cpu_info = self._cpu_static()
        psutil.cpu_percent(interval=None)
        last_t = time.monotonic()
        try:
            io = psutil.disk_io_counters(perdisk=False)
            net = psutil.net_io_counters()
        except Exception:
            io, net = None, None
        prev_io = (io.read_bytes, io.write_bytes) if io else (0, 0)
        prev_net = (net.bytes_recv, net.bytes_sent) if net else (0, 0)

        while not self._stop.is_set():
            now = time.monotonic()
            dt = max(0.05, now - last_t)
            last_t = now

            snap = Snapshot(ts=time.time())
            snap.cpu = float(psutil.cpu_percent(interval=None))
            vm = psutil.virtual_memory()
            snap.mem_pct = float(vm.percent)
            snap.mem_used = int(vm.used)
            snap.mem_total = int(vm.total)
            snap.cores, snap.threads, snap.cpu_freq = cpu_info
            snap.gpu_name = self._gpu_name

            try:
                freq = psutil.cpu_freq()
                if freq and freq.current:
                    snap.cpu_freq = float(freq.current) / 1000.0
            except Exception:
                pass

            if self._gpu_counter:
                g = self._gpu_counter.read()
                snap.gpu = None if g is None else max(0.0, min(100.0, g))

            if self._disk_idle:
                idle = self._disk_idle.read()
                snap.disk_pct = 0.0 if idle is None else max(0.0, min(100.0, 100.0 - idle))

            try:
                io = psutil.disk_io_counters(perdisk=False)
                if io:
                    rb = max(0, io.read_bytes - prev_io[0])
                    wb = max(0, io.write_bytes - prev_io[1])
                    prev_io = (io.read_bytes, io.write_bytes)
                    snap.disk_read = rb / dt
                    snap.disk_write = wb / dt
            except Exception:
                pass

            try:
                net = psutil.net_io_counters()
                if net:
                    rx = max(0, net.bytes_recv - prev_net[0])
                    tx = max(0, net.bytes_sent - prev_net[1])
                    prev_net = (net.bytes_recv, net.bytes_sent)
                    snap.net_down = rx / dt
                    snap.net_up = tx / dt
            except Exception:
                pass

            with self._lock:
                self.snapshot = snap
                self.history.push(snap)

            self._stop.wait(self.interval)

    @staticmethod
    def _cpu_static():
        cores = psutil.cpu_count(logical=False) or 1
        threads = psutil.cpu_count(logical=True) or cores
        freq = 0.0
        try:
            f = psutil.cpu_freq()
            if f:
                freq = float(f.current) / 1000.0
        except Exception:
            pass
        return cores, threads, freq

    # -------------------------------------------------- 读取
    def read(self):
        with self._lock:
            return self.snapshot, {
                "cpu": list(self.history.cpu),
                "mem": list(self.history.mem),
                "gpu": list(self.history.gpu),
                "disk": list(self.history.disk),
                "net_rx": list(self.history.net_rx),
                "net_tx": list(self.history.net_tx),
            }


def gpu_display_name() -> str:
    """从注册表读取显示适配器名称（失败返回空串）。"""
    import winreg
    base = (r"SYSTEM\CurrentControlSet\Control\Class"
            r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
    best = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for i in range(16):
                try:
                    with winreg.OpenKey(root, f"{i:04d}") as sub:
                        desc = str(winreg.QueryValueEx(sub, "DriverDesc")[0]).strip()
                        if desc:
                            best = desc
                            break
                except OSError:
                    continue
    except OSError:
        return ""
    for token in ("Intel(R) ", "Intel "):
        if best.startswith(token):
            best = best[len(token):]
            break
    low = best.lower()
    if "basic display" in low or "basic render" in low or "remote display" in low:
        return ""
    return best


def format_bytes(n: float, suffix: str = "") -> str:
    if n < 1024:
        return f"{n:.0f} B{suffix}"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            if n >= 100:
                return f"{n:.0f} {unit}{suffix}"
            if n >= 10:
                return f"{n:.1f} {unit}{suffix}"
            return f"{n:.2f} {unit}{suffix}"
    return f"{n:.1f} TB{suffix}"


def format_rate(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B/s"
    return format_bytes(n) + "/s"
