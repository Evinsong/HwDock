"""生成 README 用截图：真实采集数据 + 脱敏（主机名 / IP 替换为通用值）。

用法：python tools/make_readme_shots.py
输出：assets/preview.png（竖版 132%）、assets/preview_top.png（横版 132%）
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hwdock.hardware import HardwareHub
from hwdock.metrics import Sampler
from hwdock.render import EDGE_RIGHT, EDGE_TOP, PanelState, Renderer
from hwdock.sensors import SensorHub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")
SCALE = 1.32
FAKE_HOST = "DESKTOP-8H2K9M"
FAKE_IP = "192.168.1.100"


def sanitize(info):
    """抹掉可识别信息：主机名与内网 IP。"""
    try:
        info.host_name = FAKE_HOST
        for n in getattr(info, "nets", []) or []:
            if getattr(n, "ips", None):
                n.ips = [FAKE_IP]
            if getattr(n, "mac", ""):
                n.mac = "00:00:00:00:00:00"
    except Exception:
        pass
    return info


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    sampler = Sampler(1.0)
    sensors = SensorHub()
    hardware = HardwareHub()
    for h in (sampler, sensors, hardware):
        h.start()
    for _ in range(6):
        time.sleep(0.25)
        sampler.read()
    time.sleep(4.5)

    snap, hist = sampler.read()
    state = PanelState(
        snap=snap, hist=hist, hostname=FAKE_HOST,
        clock="13:24:07", edge=EDGE_RIGHT, scale=SCALE,
        sensors=sensors.read(), hw=sanitize(hardware.read()), admin=False,
    )
    r = Renderer(SCALE)

    v = r.render_panel(state)
    v.save(os.path.join(OUT, "preview.png"))
    print("preview.png", v.size)

    state.edge = EDGE_TOP
    state.horizontal = True
    h = r.render_panel(state)
    h.save(os.path.join(OUT, "preview_top.png"))
    print("preview_top.png", h.size)

    for hub in (sampler, sensors, hardware):
        hub.stop()


if __name__ == "__main__":
    main()
