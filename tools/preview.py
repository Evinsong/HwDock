"""离屏预览：用真实采集数据渲染面板 PNG，便于检查视觉细节。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hwdock.hardware import HardwareHub
from hwdock.metrics import Sampler
from hwdock.render import EDGE_NONE, EDGE_RIGHT, EDGE_TOP, PanelState, Renderer
from hwdock.sensors import SensorHub

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build")


def main() -> None:
    scales = [float(x) for x in sys.argv[1:]] or [1.0]
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
    for sc in scales:
        tag = f"_{int(round(sc * 100))}"
        state = PanelState(
            snap=snap, hist=hist, hostname=os.environ.get("COMPUTERNAME", "PC"),
            clock=time.strftime("%H:%M:%S"), edge=EDGE_RIGHT, scale=sc,
            sensors=sensors.read(), hw=hardware.read(), admin=True,
        )
        r = Renderer(sc)
        v = r.render_panel(state)
        v.save(os.path.join(OUT, f"preview_v{tag}.png"))
        print(f"[{sc:.2f}] vertical", v.size)

        state.edge = EDGE_TOP
        state.horizontal = True
        h = r.render_panel(state)
        h.save(os.path.join(OUT, f"preview_h{tag}.png"))
        print(f"[{sc:.2f}] horizontal", h.size)

        state.edge = EDGE_RIGHT
        state.horizontal = False
        s = r.render_strip(state)
        s.save(os.path.join(OUT, f"preview_strip{tag}.png"))
        print(f"[{sc:.2f}] strip", s.size)

        state.edge = EDGE_NONE
        r.render_panel(state)
        t = time.perf_counter()
        for _ in range(20):
            r.render_panel(state)
        print(f"[{sc:.2f}] render avg ms",
              round((time.perf_counter() - t) / 20 * 1000, 2))

    for hub in (sampler, sensors, hardware):
        hub.stop()


if __name__ == "__main__":
    main()
