"""硬件型号与静态信息采集（WMI 为主，注册表/psutil 兜底）。

只在启动时与每 5 分钟刷新一次，避免持续占用。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import psutil

from .metrics import format_bytes
from .sensors import Sensors, is_admin, storage_models
from .wmi import Wmi

try:
    import winreg
except Exception:  # pragma: no cover
    winreg = None


# ------------------------------------------------------------------ 数据结构
@dataclass
class MemModule:
    slot: str = ""
    size_gb: float = 0.0
    speed: int = 0
    maker: str = ""
    part: str = ""
    kind: str = ""

    def label(self) -> str:
        bits = [f"{self.size_gb:.0f}GB"]
        if self.kind:
            bits.append(self.kind)
        if self.speed:
            bits.append(f"{self.speed}")
        text = " ".join(bits)
        extra = " ".join(x for x in (self.maker, self.part) if x)
        return f"{self.slot} {text} {extra}".strip()


@dataclass
class GpuInfo:
    name: str = ""
    vram: int = 0
    driver: str = ""
    resolution: str = ""


@dataclass
class DiskInfo:
    index: int = 0
    model: str = ""
    size: int = 0
    bus: str = ""
    temp: Optional[float] = None


@dataclass
class NetItem:
    name: str = ""
    conn: str = ""
    speed: int = 0
    ips: list = field(default_factory=list)
    mac: str = ""


@dataclass
class HardwareInfo:
    host_name: str = ""
    host_model: str = ""
    board: str = ""
    bios: str = ""
    os_name: str = ""
    cpu_name: str = ""
    cores: int = 0
    threads: int = 0
    max_mhz: int = 0
    base_mhz: int = 0
    l3_mb: float = 0.0
    socket: str = ""
    mem_total: int = 0
    mem_modules: list = field(default_factory=list)
    gpus: list = field(default_factory=list)
    disks: list = field(default_factory=list)
    nets: list = field(default_factory=list)
    battery: str = ""
    wmi_ok: bool = False
    collected: float = 0.0


# ------------------------------------------------------------------ 工具
_DDR_TYPES = {
    18: "DDR", 19: "DDR2", 20: "DDR2", 24: "DDR3", 26: "DDR4",
    27: "LPDDR", 28: "LPDDR2", 29: "LPDDR3", 30: "LPDDR4", 34: "DDR5",
    35: "LPDDR5",
}


def clean_cpu_name(name: str) -> str:
    for token in ("(R)", "(TM)", "(tm)", "CPU ", "Processor"):
        name = name.replace(token, "")
    return " ".join(name.split())


_MAKER_NOISE = ("COMPUTER INC.", "COMPUTER, INC.", "COMPUTER INC", "INC.",
                "CO., LTD.", "CO.,LTD.", "CORPORATION", "LTD.", "TECHNOLOGY",
                "MICRO-STAR", "INTERNATIONAL")


def _short_maker(maker: Optional[str], product: Optional[str]) -> str:
    """把 "ASUSTeK COMPUTER INC. EX-B760M-V5" 压成 "ASUSTeK EX-B760M-V5"。"""
    maker = " ".join(str(maker or "").split())
    product = " ".join(str(product or "").split())
    for token in _MAKER_NOISE:
        maker = maker.replace(token, " ")
    maker = " ".join(maker.split())
    if not product:
        return maker
    if not maker:
        return product
    if product.lower().startswith(maker.lower()):
        return product
    return f"{maker} {product}"


def clean_adapter_name(name: str) -> str:
    for token in ("(R)", "(TM)", "(r)", "(tm)"):
        name = name.replace(token, "")
    return " ".join(name.split())


def short_os(caption: str, build: str) -> str:
    text = (caption or "").replace("Microsoft ", "").replace("Windows ", "Windows ")
    text = " ".join(text.split())
    if "Windows" not in text:
        text = f"Windows {text}".strip()
    if build:
        text = f"{text} · {build}"
    return text


def fmt_speed_bps(bps: Optional[int]) -> str:
    if not bps:
        return ""
    if bps >= 1_000_000_000:
        v = bps / 1_000_000_000
        return f"{v:.1f} Gbps" if v < 10 else f"{v:.0f} Gbps"
    return f"{bps / 1_000_000:.0f} Mbps"


def _reg(path: str, value: str) -> str:
    if not winreg:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return str(winreg.QueryValueEx(key, value)[0]).strip()
    except Exception:
        return ""


# ------------------------------------------------------------------ 采集
def collect_hardware() -> HardwareInfo:
    info = HardwareInfo(host_name=os.environ.get("COMPUTERNAME", ""), collected=time.time())
    w = Wmi()
    info.wmi_ok = w.ok
    try:
        if w.ok:
            _from_wmi(info, w)
    finally:
        w.close()
    _fallbacks(info)
    return info


def _from_wmi(info: HardwareInfo, w: Wmi) -> None:
    # 主机 / 主板 / BIOS
    rows = w.query(
        "SELECT Manufacturer, Model, SystemFamily, TotalPhysicalMemory "
        "FROM Win32_ComputerSystem",
        ["Manufacturer", "Model", "SystemFamily", "TotalPhysicalMemory"],
    )
    if rows:
        r = rows[0]
        maker = (r.get("Manufacturer") or "").strip()
        model = (r.get("Model") or "").strip()
        info.host_model = " ".join(x for x in (maker, model) if x)
        info.mem_total = int(r.get("TotalPhysicalMemory") or 0)

    rows = w.query("SELECT Manufacturer, Product, Version FROM Win32_BaseBoard",
                   ["Manufacturer", "Product", "Version"])
    if rows:
        r = rows[0]
        info.board = _short_maker(r.get("Manufacturer"), r.get("Product"))

    rows = w.query("SELECT Manufacturer, SMBIOSBIOSVersion, ReleaseDate FROM Win32_BIOS",
                   ["Manufacturer", "SMBIOSBIOSVersion", "ReleaseDate"])
    if rows:
        r = rows[0]
        ver = (r.get("SMBIOSBIOSVersion") or "").strip()
        date = str(r.get("ReleaseDate") or "")
        year = date[:4] if date[:4].isdigit() else ""
        info.bios = " ".join(x for x in (ver, year) if x)

    rows = w.query("SELECT Caption, BuildNumber, Version FROM Win32_OperatingSystem",
                   ["Caption", "BuildNumber", "Version"])
    if rows:
        info.os_name = short_os(rows[0].get("Caption") or "", rows[0].get("BuildNumber") or "")

    # CPU
    rows = w.query(
        "SELECT Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed, "
        "CurrentClockSpeed, L2CacheSize, L3CacheSize, SocketDesignation "
        "FROM Win32_Processor",
        ["Name", "NumberOfCores", "NumberOfLogicalProcessors", "MaxClockSpeed",
         "CurrentClockSpeed", "L2CacheSize", "L3CacheSize", "SocketDesignation"],
    )
    if rows:
        r = rows[0]
        info.cpu_name = clean_cpu_name(str(r.get("Name") or ""))
        info.cores = int(r.get("NumberOfCores") or 0)
        info.threads = int(r.get("NumberOfLogicalProcessors") or 0)
        info.max_mhz = int(r.get("MaxClockSpeed") or 0)
        info.base_mhz = int(r.get("CurrentClockSpeed") or 0)
        info.l3_mb = round(float(r.get("L3CacheSize") or 0) / 1024.0, 1)
        info.socket = str(r.get("SocketDesignation") or "")

    # 内存条
    rows = w.query(
        "SELECT Capacity, Speed, ConfiguredClockSpeed, Manufacturer, PartNumber, "
        "DeviceLocator, SMBIOSMemoryType FROM Win32_PhysicalMemory",
        ["Capacity", "Speed", "ConfiguredClockSpeed", "Manufacturer", "PartNumber",
         "DeviceLocator", "SMBIOSMemoryType"],
    )
    total = 0
    for r in rows:
        try:
            cap = int(r.get("Capacity") or 0)
        except Exception:
            cap = 0
        total += cap
        mtype = r.get("SMBIOSMemoryType")
        info.mem_modules.append(MemModule(
            slot=str(r.get("DeviceLocator") or "").replace("Controller", "C"),
            size_gb=cap / 1024 ** 3,
            speed=int(r.get("ConfiguredClockSpeed") or r.get("Speed") or 0),
            maker=str(r.get("Manufacturer") or "").strip(),
            part=str(r.get("PartNumber") or "").strip(),
            kind=_DDR_TYPES.get(mtype if isinstance(mtype, int) else -1, ""),
        ))
    if total:
        info.mem_total = total

    # 显卡
    rows = w.query(
        "SELECT Name, AdapterRAM, DriverVersion, CurrentHorizontalResolution, "
        "CurrentVerticalResolution, CurrentRefreshRate FROM Win32_VideoController",
        ["Name", "AdapterRAM", "DriverVersion", "CurrentHorizontalResolution",
         "CurrentVerticalResolution", "CurrentRefreshRate"],
    )
    for r in rows:
        name = str(r.get("Name") or "").strip()
        if not name or "Basic Display" in name or "Basic Render" in name:
            continue
        vram = r.get("AdapterRAM") or 0
        vram = int(vram) if isinstance(vram, int) and vram > 0 else 0
        hz = r.get("CurrentHorizontalResolution")
        vt = r.get("CurrentVerticalResolution")
        res = f"{hz}×{vt}" if hz and vt else ""
        info.gpus.append(GpuInfo(
            name=name, vram=vram, driver=str(r.get("DriverVersion") or ""), resolution=res,
        ))

    # 硬盘
    rows = w.query(
        "SELECT Index, Model, Size, InterfaceType, MediaType, SerialNumber "
        "FROM Win32_DiskDrive",
        ["Index", "Model", "Size", "InterfaceType", "MediaType", "SerialNumber"],
    )
    models = storage_models()
    for r in rows:
        try:
            size = int(r.get("Size") or 0)
        except Exception:
            size = 0
        idx = int(r.get("Index") or 0)
        model = " ".join(str(r.get("Model") or "").strip().strip("'\"").split())
        if not model:
            model = " ".join(models.get(idx, "").strip().strip("'\"").split())
        bus = str(r.get("InterfaceType") or "").strip()
        info.disks.append(DiskInfo(index=idx, model=model, size=size, bus=bus))

    # 网卡（只取物理网卡）
    rows = w.query(
        "SELECT Name, NetConnectionID, Speed, MACAddress, NetEnabled "
        "FROM Win32_NetworkAdapter WHERE PhysicalAdapter=TRUE",
        ["Name", "NetConnectionID", "Speed", "MACAddress", "NetEnabled"],
    )
    addrs = _net_addresses()
    for r in rows:
        conn = str(r.get("NetConnectionID") or "").strip()
        name = str(r.get("Name") or "").strip()
        if not name:
            continue
        ips = addrs.get(conn, [])
        if not ips and not r.get("NetEnabled"):
            continue
        info.nets.append(NetItem(
            name=clean_adapter_name(name), conn=conn, speed=int(r.get("Speed") or 0),
            ips=ips, mac=str(r.get("MACAddress") or ""),
        ))

    # 电池
    rows = w.query("SELECT Name, EstimatedChargeRemaining FROM Win32_Battery",
                   ["Name", "EstimatedChargeRemaining"])
    if rows:
        r = rows[0]
        pct = r.get("EstimatedChargeRemaining")
        info.battery = f"{pct}%" if pct is not None else ""


def _net_addresses() -> dict[str, list[str]]:
    """按连接名（以太网/WLAN）归集 IPv4 地址。"""
    out: dict[str, list[str]] = {}
    try:
        stats = psutil.net_if_addrs()
    except Exception:
        return out
    for name, items in stats.items():
        ips = []
        for it in items:
            if it.family == 2 and not it.address.startswith("169.254"):
                ips.append(it.address)
        if ips:
            out[name] = ips
    return out


def _fallbacks(info: HardwareInfo) -> None:
    """WMI 不可用时用注册表与 psutil 补齐关键字段。"""
    if not info.cpu_name:
        info.cpu_name = clean_cpu_name(_reg(
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString"))
    if not info.host_model:
        maker = _reg(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemManufacturer")
        model = _reg(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName")
        info.host_model = " ".join(x for x in (maker, model) if x)
    if not info.board:
        maker = _reg(r"HARDWARE\DESCRIPTION\System\BIOS", "BaseBoardManufacturer")
        product = _reg(r"HARDWARE\DESCRIPTION\System\BIOS", "BaseBoardProduct")
        info.board = _short_maker(maker, product)
    if not info.bios:
        info.bios = _reg(r"HARDWARE\DESCRIPTION\System\BIOS", "BIOSVersion")
    if not info.os_name:
        try:
            import platform
            info.os_name = f"Windows {platform.release()}"
        except Exception:
            info.os_name = "Windows"
    if not info.cores:
        info.cores = psutil.cpu_count(logical=False) or 0
    if not info.threads:
        info.threads = psutil.cpu_count(logical=True) or 0
    if not info.mem_total:
        try:
            info.mem_total = int(psutil.virtual_memory().total)
        except Exception:
            pass
    if not info.disks:
        for i, part in enumerate(psutil.disk_partitions(all=False)):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            info.disks.append(DiskInfo(index=i, model=part.device, size=usage.total,
                                       bus=part.fstype))
    if not info.gpus:
        from .metrics import gpu_display_name
        name = gpu_display_name()
        if name:
            info.gpus.append(GpuInfo(name=name))
    if not info.nets:
        for name, ips in _net_addresses().items():
            info.nets.append(NetItem(name=name, conn=name, ips=ips))


# ------------------------------------------------------------------ 后台刷新
class HardwareHub:
    """静态硬件信息只在启动与每 5 分钟刷新，温度由 SensorHub 提供。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.info = HardwareInfo()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="hardware", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def read(self) -> HardwareInfo:
        with self._lock:
            return self.info

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_once()
            self._stop.wait(300.0)

    def refresh_once(self) -> None:
        """立即重新采集一次（菜单“重新扫描硬件”）。"""
        try:
            data = collect_hardware()
        except Exception:
            return
        with self._lock:
            self.info = data


# ------------------------------------------------------------------ 展示卡片
@dataclass
class HwCard:
    title: str
    color: tuple
    name: str
    lines: list
    temp: Optional[float] = None
    temp_note: str = ""


def fmt_gb(nbytes: float) -> str:
    """容量显示：整 GB 不带小数，便于阅读。"""
    gb = (nbytes or 0) / 1024 ** 3
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    if abs(gb - round(gb)) < 0.06:
        return f"{round(gb):.0f} GB"
    return f"{gb:.1f} GB"


def build_hw_cards(info: HardwareInfo, sens: Sensors) -> list[HwCard]:
    """把硬件信息整理成六张卡片（型号 + 关键参数 + 温度）。"""
    accent = _accents()

    # 系统 / 主板
    name = info.host_model or info.host_name or "本机"
    sys_lines = []
    if info.board:
        sys_lines.append(f"主板 {info.board}")
    bios_line = f"BIOS {info.bios}" if info.bios else ""
    if info.os_name:
        bios_line = f"{bios_line} · {info.os_name}" if bios_line else info.os_name
    if bios_line:
        sys_lines.append(bios_line)
    sys_lines.append(f"{info.host_name} · 内存 {fmt_gb(info.mem_total)}"
                     + (f" · 电池 {info.battery}" if info.battery else ""))
    cards = [HwCard("系统 · 主板", accent["sys"], name, sys_lines,
                    sens.board.value if sens.board else None)]

    # CPU
    cpu_lines = []
    if info.cores or info.threads:
        cpu_lines.append(" · ".join(x for x in (
            f"{info.cores} 核 {info.threads} 线程" if info.cores else "",
            f"L3 {info.l3_mb:g} MB" if info.l3_mb else "") if x))
    if sens.cpu:
        src = "ACPI 温区" if sens.cpu.source == "ACPI" else sens.cpu.source
        cpu_lines.append(f"温度 {sens.cpu.value:.0f}°C · 来源 {src}")
    if info.max_mhz:
        line = f"主频 {info.max_mhz / 1000:.2f} GHz"
        if info.socket:
            line = f"{info.socket} · {line}"
        cpu_lines.append(line)
    cards.append(HwCard("处理器", accent["cpu"], info.cpu_name or "未知处理器",
                        cpu_lines, sens.cpu.value if sens.cpu else None,
                        sens.cpu.source if sens.cpu else ""))

    # 内存
    total = info.mem_total
    kind = next((m.kind for m in info.mem_modules if m.kind), "")
    speed = max((m.speed for m in info.mem_modules), default=0)
    spec = "-".join(x for x in (kind, str(speed) if speed else "") if x)
    mem_name = f"{fmt_gb(total)} {spec}".strip() if total else "未知"
    mem_lines = []
    if info.mem_modules:
        n = len(info.mem_modules)
        mem_lines.append(f"{'双通道' if n % 2 == 0 and n > 1 else '单通道'} · {n} 条")
        for m in info.mem_modules[:3]:
            mem_lines.append(m.label())
    else:
        mem_lines.append("未读取到内存条信息")
    cards.append(HwCard("内存", accent["mem"], mem_name, mem_lines))

    # GPU
    if info.gpus:
        g = info.gpus[0]
        vram = sens.gpu_mem_total or g.vram
        lines = [" · ".join(x for x in (
            fmt_gb(vram) if vram else "",
            f"驱动 {g.driver}" if g.driver else "") if x)]
        tail = " · ".join(x for x in (
            g.resolution,
            f"占用 {sens.gpu_util:.0f}%" if sens.gpu_util is not None else "") if x)
        if tail:
            lines.append(tail)
        if sens.gpu_mem_total and sens.gpu_mem_used:
            lines.append(f"显存 {fmt_gb(sens.gpu_mem_used)} / {fmt_gb(sens.gpu_mem_total)}")
        if len(info.gpus) > 1:
            lines.append(f"另有 {len(info.gpus) - 1} 个显示适配器")
        cards.append(HwCard("显卡", accent["gpu"], g.name, lines,
                            sens.gpu.value if sens.gpu else None,
                            sens.gpu.source if sens.gpu else ""))
    else:
        cards.append(HwCard("显卡", accent["gpu"], "未检测到独立显卡", []))

    # 存储
    total_size = sum(d.size for d in info.disks)
    name = (f"{len(info.disks)} 块 · {fmt_gb(total_size)}" if info.disks
            else "未检测到磁盘")
    lines = []
    for d in info.disks[:3]:
        bits = [f"磁盘{d.index}" if len(info.disks) > 1 else "",
                d.model or "磁盘", fmt_gb(d.size) if d.size else "", d.bus]
        line = " · ".join(x for x in bits if x)
        if sens.disk:
            line = f"{line} · {sens.disk.value:.0f}°C"
        else:
            line = f"{line} · 温度 --"
        lines.append(line)
    if len(info.disks) > 3:
        lines.append(f"另有 {len(info.disks) - 3} 块磁盘")
    cards.append(HwCard("存储", accent["disk"], name, lines,
                        sens.disk.value if sens.disk else None,
                        sens.disk.source if sens.disk else ""))

    # 网络
    if info.nets:
        n = info.nets[0]
        lines = []
        speed = fmt_speed_bps(n.speed)
        ip = n.ips[0] if n.ips else ""
        line = " · ".join(x for x in (speed, ip) if x)
        if line:
            lines.append(line)
        tail = " · ".join(x for x in (n.conn or "网络", n.mac) if x)
        if tail:
            lines.append(tail)
        if len(info.nets) > 1:
            lines.append(f"另有 {len(info.nets) - 1} 个物理网卡")
        cards.append(HwCard("网络", accent["net"], n.name, lines))
    else:
        cards.append(HwCard("网络", accent["net"], "未检测到物理网卡", []))

    return cards


def _disk_temps(sens: Sensors, info: HardwareInfo) -> dict:
    out = {}
    if sens.disk:
        for d in info.disks:
            out[d.index] = sens.disk.value
    return out


def _accents() -> dict:
    from .render import ACCENT
    return {
        "sys": (140, 160, 190),
        "cpu": ACCENT["cpu"], "mem": ACCENT["mem"], "gpu": ACCENT["gpu"],
        "disk": ACCENT["disk"], "net": ACCENT["net"],
    }


def hardware_summary(info: HardwareInfo, sens: Sensors) -> str:
    """纯文本摘要，供“复制硬件信息”使用。"""
    lines = [f"=== {info.host_name} 硬件信息 ==="]
    lines.append(f"主机型号: {info.host_model or '-'}")
    lines.append(f"主板: {info.board or '-'}   BIOS: {info.bios or '-'}")
    lines.append(f"操作系统: {info.os_name or '-'}")
    cpu = f"{info.cpu_name} / {info.cores}核{info.threads}线程"
    if info.max_mhz:
        cpu += f" / {info.max_mhz / 1000:.2f}GHz"
    if info.l3_mb:
        cpu += f" / L3 {info.l3_mb:g}MB"
    if info.socket:
        cpu += f" / {info.socket}"
    lines.append(f"处理器: {cpu}")
    if sens.cpu:
        lines.append(f"  CPU 温度: {sens.cpu.value:.0f}°C（{sens.cpu.source} · {sens.cpu.label}）")
    lines.append(f"内存: {format_bytes(info.mem_total)} / {len(info.mem_modules)} 条")
    for m in info.mem_modules:
        lines.append(f"  {m.label()}")
    for g in info.gpus:
        t = f" {sens.gpu.value:.0f}°C" if sens.gpu else ""
        lines.append(f"显卡: {g.name} / 驱动 {g.driver}{t}")
    for d in info.disks:
        t = f" {sens.disk.value:.0f}°C" if sens.disk else ""
        lines.append(f"存储: [{d.index}] {d.model} {format_bytes(d.size)}{t}")
    for n in info.nets:
        speed = fmt_speed_bps(n.speed)
        lines.append(f"网络: {n.name} / {n.conn} / {speed} / {', '.join(n.ips)}")
    if info.battery:
        lines.append(f"电池: {info.battery}")
    if sens.sources:
        lines.append(f"温度来源: {', '.join(sens.sources)}")
    if not is_admin():
        lines.append("提示: 当前非管理员权限，硬盘温度等传感器可能不可用。")
    return "\n".join(lines)
