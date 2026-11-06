"""窗口主体：分层透明窗口、边缘吸附、悬停展开、右键菜单、托盘。"""
from __future__ import annotations

import ctypes
import math
import os
import socket
import sys
import threading
import time
import traceback
from ctypes import wintypes as wt

from PIL import Image, ImageChops

from . import win32core as wc
from .config import Config, autostart_enabled, set_autostart, log_path
from .hardware import HardwareHub, hardware_summary
from .icon import tray_icon_image
from .metrics import Sampler
from .render import (
    EDGE_BOTTOM, EDGE_LEFT, EDGE_NONE, EDGE_RIGHT, EDGE_TOP, Layout, PAD,
    STRIP_W, PanelState, Renderer, header_button_rects, header_drag_rect,
)
from .sensors import SensorHub, is_admin

CLASS_NAME = "HwDock_Panel_v1"
TIMER_POLL = 1
TIMER_DATA = 2
TIMER_ANIM = 3
WM_TRAY = wc.WM_APP + 10
WM_SHOW = wc.WM_APP + 50

EDGES = [
    ("吸附到左侧", EDGE_LEFT),
    ("吸附到右侧", EDGE_RIGHT),
    ("吸附到顶部", EDGE_TOP),
    ("吸附到底部", EDGE_BOTTOM),
    ("自由浮动", EDGE_NONE),
]

# 菜单命令
CMD_EDGE_BASE = 200
CMD_AUTOHIDE = 210
CMD_AUTOSTART = 211
CMD_INTERVAL_BASE = 220
CMD_OPACITY_BASE = 230
CMD_SCALE_BASE = 270
CMD_COPY_HW = 240
CMD_REFRESH_HW = 241
CMD_ELEVATE = 242
CMD_HIDE = 260
CMD_ABOUT = 250
CMD_QUIT = 299
INTERVALS = [0.5, 1.0, 2.0, 5.0]
OPACITIES = [1.0, 0.95, 0.9, 0.85]
UI_SCALES = [0.0, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0]

_gdi = wc.gdi32
wc.user32.SetCapture.argtypes = [wt.HWND]
wc.user32.ReleaseCapture.argtypes = []
wc.user32.GetSystemMetrics.restype = ctypes.c_int


def _premultiply(img: Image.Image, alpha: float = 1.0) -> Image.Image:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    r, g, b, a = img.split()
    if alpha < 0.999:
        a = a.point(lambda v: int(v * alpha))
    return Image.merge("RGBA", (
        ImageChops.multiply(r, a),
        ImageChops.multiply(g, a),
        ImageChops.multiply(b, a),
        a,
    ))


def _log(msg: str) -> None:
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _set_clipboard(text: str) -> bool:
    """写入剪贴板（Unicode 文本）。"""
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(handle)
        return bool(u32.SetClipboardData(CF_UNICODETEXT, handle))
    except Exception:
        return False
    finally:
        u32.CloseClipboard()


class DockApp:
    def __init__(self, cfg: Config | None = None):
        wc.set_dpi_aware()
        self.cfg = cfg or Config.load()
        self.hostname = socket.gethostname()
        self.scale = self._calc_scale()
        self.renderer = Renderer(self.scale)
        self.sampler = Sampler(self.cfg.interval)
        self.sensors = SensorHub()
        self.hardware = HardwareHub()
        self.surface = wc.Surface()
        self.hwnd = None
        self._wndproc_ref = None

        self.vis = 1.0
        self.anim = None
        self.img_panel: Image.Image | None = None
        self.img_strip: Image.Image | None = None
        self.hover_btn = -1
        self.admin = is_admin()
        self.dragging = False
        self._drag_from = (0, 0)
        self._drag_win = (0, 0)
        self._drag_orient = False
        self.menu_open = False
        self.mouse_away_since: float | None = None
        self._dock_cool = 0.0
        self.clock = ""
        self._last_ts = 0.0
        self._sens_ts = 0.0
        self._sens_sig = None
        self._hw_ts = 0.0
        self.rect = (0, 0, 0, 0)
        self.crop_off = (0, 0)
        self.tray = None
        self.hidden = False
        self._first_hide_tip = True
        self._screen_dc = None

    # ------------------------------------------------------------ 基础
    def _calc_scale(self) -> float:
        """界面缩放：跟随显示器像素密度自动推算，也可由配置手动锁定。

        100% 基准对应 1920×1080 + 96 DPI；2K/4K 屏即便系统缩放留在
        100%，面板与字号也会按像素密度等比放大，避免字小到看不清。
        """
        ax, ay = self._anchor()
        wl, wt_, wr, wb = wc.monitor_work_area(ax, ay)
        ww, wh = max(1, wr - wl), max(1, wb - wt_)

        manual = self.cfg.ui_scale
        if manual > 0:
            s = manual
        else:
            dpi_s = wc.monitor_dpi(ax, ay) / 96.0
            px_s = math.sqrt((ww * wh) / (1920.0 * 1080.0))
            s = max(dpi_s, px_s)

        # 面板不得超出工作区（竖向面板按高、横向面板按宽取约束）
        s = min(s, wh * 0.94 / Layout(1.0, False).window_h,
                ww * 0.96 / Layout(1.0, True).window_w)
        return round(max(0.85, min(2.2, s)), 3)

    def _apply_scale(self, persist: bool = False) -> None:
        new = self._calc_scale()
        if abs(new - self.scale) > 0.001:
            self.scale = new
            self.renderer = Renderer(self.scale)
            self._invalidate()
            self._present()
        if persist:
            self.cfg.save()

    def _layout(self) -> Layout:
        if self.dragging:
            return Layout(self.scale, self._drag_orient)
        return Layout(self.scale, self.cfg.edge in (EDGE_TOP, EDGE_BOTTOM))

    def _anchor(self) -> tuple[int, int]:
        ax, ay = self.cfg["anchor_x"], self.cfg["anchor_y"]
        if ax is None or ay is None:
            return (wc.user32.GetSystemMetrics(0) // 2,
                    wc.user32.GetSystemMetrics(1) // 2)
        return int(ax), int(ay)

    def _work_area(self) -> tuple[int, int, int, int]:
        ax, ay = self._anchor()
        return wc.monitor_work_area(ax, ay)

    def _invalidate(self) -> None:
        self.img_panel = None
        self.img_strip = None

    # ------------------------------------------------------------ 窗口
    def _create_window(self) -> None:
        hinst = wc.kernel32.GetModuleHandleW(None)
        self._wndproc_ref = wc.WNDPROC(self._wndproc)
        wcx = wc.WNDCLASSEXW()
        wcx.cbSize = ctypes.sizeof(wc.WNDCLASSEXW)
        wcx.style = 0
        wcx.lpfnWndProc = self._wndproc_ref
        wcx.hInstance = hinst
        wcx.hCursor = wc.cursor_handle(wc.IDC_ARROW)
        wcx.lpszClassName = CLASS_NAME
        wc.user32.RegisterClassExW(ctypes.byref(wcx))

        ex = (wc.WS_EX_LAYERED | wc.WS_EX_TOOLWINDOW | wc.WS_EX_NOACTIVATE
              | wc.WS_EX_TOPMOST)
        self.hwnd = wc.user32.CreateWindowExW(
            ex, CLASS_NAME, "HwDock 硬件监视", wc.WS_POPUP,
            0, 0, 100, 100, None, None, hinst, None,
        )
        if not self.hwnd:
            raise RuntimeError("CreateWindowEx 失败")
        wc.user32.SetTimer(self.hwnd, TIMER_POLL, 80, None)
        wc.user32.SetTimer(self.hwnd, TIMER_DATA, 300, None)

    def run(self) -> None:
        self.sampler.start()
        self.sensors.start()
        self.hardware.start()
        self._create_window()
        self._start_tray()
        if self.cfg.edge != EDGE_NONE and self.cfg.auto_hide:
            self.vis = 0.0
        self._invalidate()
        self._present()
        self._message_loop()

    def _message_loop(self) -> None:
        msg = wt.MSG()
        while wc.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            wc.user32.TranslateMessage(ctypes.byref(msg))
            wc.user32.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------ 呈现
    def _panel_state(self) -> PanelState:
        snap, hist = self.sampler.read()
        lay = self._layout()
        return PanelState(
            snap=snap, hist=hist, hostname=self.hostname, clock=self.clock,
            edge=self.cfg.edge, scale=self.scale, hover_btn=self.hover_btn,
            interval=self.cfg.interval, sensors=self.sensors.read(),
            hw=self.hardware.read(), admin=self.admin, dragging=self.dragging,
            horizontal=lay.horizontal,
        )

    def _get_panel(self) -> Image.Image:
        if self.img_panel is None:
            self.img_panel = self.renderer.render_panel(self._panel_state())
        return self.img_panel

    def _get_strip(self) -> Image.Image:
        if self.img_strip is None:
            self.img_strip = self.renderer.render_strip(self._panel_state())
        return self.img_strip

    def _present(self) -> None:
        if not self.hwnd:
            return
        try:
            img, x, y = self._compose()
        except Exception:
            _log("compose 失败:\n" + traceback.format_exc())
            return
        if img is None or img.width < 1 or img.height < 1:
            return
        self.rect = (x, y, x + img.width, y + img.height)
        data = _premultiply(img, self.cfg.opacity).tobytes("raw", "BGRA")
        self.surface.ensure(img.width, img.height)
        self.surface.blit_bgra(data)
        if self._screen_dc is None:
            self._screen_dc = wc.user32.GetDC(None)
        pt_dst = wt.POINT(int(x), int(y))
        size = wt.SIZE(img.width, img.height)
        pt_src = wt.POINT(0, 0)
        blend = wc.BLENDFUNCTION(wc.AC_SRC_OVER, 0, 255, wc.AC_SRC_ALPHA)
        wc.user32.UpdateLayeredWindow(
            self.hwnd, self._screen_dc, ctypes.byref(pt_dst), ctypes.byref(size),
            self.surface.memdc, ctypes.byref(pt_src), 0, ctypes.byref(blend),
            wc.ULW_ALPHA,
        )
        wc.user32.ShowWindow(self.hwnd, wc.SW_SHOWNOACTIVATE)

    def _compose(self):
        edge = self.cfg.edge
        lay = self._layout()
        p = lay.px(PAD)
        pw, ph = lay.panel_w, lay.panel_h
        strip_px = int(round(STRIP_W * self.scale))

        if edge == EDGE_NONE:
            self.crop_off = (0, 0)
            panel = self._get_panel()
            wl, wt_, wr, wb = self._work_area()
            fx = self.cfg["float_x"]
            fy = self.cfg["float_y"]
            if fx is None or fy is None:
                fx = wr - panel.width - 24
                fy = wt_ + ((wb - wt_) - panel.height) // 2
                self.cfg["float_x"], self.cfg["float_y"] = int(fx), int(fy)
            # 缩放变化后可能超出屏幕，夹回工作区
            fx = min(max(int(fx), wl), max(wl, wr - panel.width))
            fy = min(max(int(fy), wt_), max(wt_, wb - panel.height))
            return panel, int(fx), int(fy)

        wl, wt_, wr, wb = self._work_area()
        ww, wh = wr - wl, wb - wt_
        ratio = self.cfg.ratio

        if edge in (EDGE_LEFT, EDGE_RIGHT):
            panel_y = int(round(wt_ + (wh - ph) * ratio))
            if self.vis <= 0.02:
                strip = self._get_strip()
                self.crop_off = (0, 0)
                sw, sh = strip.size
                y = int(panel_y + ph / 2 - sh / 2)
                x = wl if edge == EDGE_LEFT else wr - sw
                return strip, int(x), int(y)
            panel = self._get_panel()
            if self.vis >= 0.999:
                self.crop_off = (0, 0)
                x = wl - p if edge == EDGE_LEFT else wr - pw - p
                return panel, int(x), int(panel_y - p)
            vw = int(round(strip_px + (pw - strip_px) * self.vis))
            if edge == EDGE_LEFT:
                self.crop_off = (0, 0)
                return panel.crop((0, 0, vw + p, panel.height)), int(wl - p), int(panel_y - p)
            offx = panel.width - (vw + p)
            self.crop_off = (offx, 0)
            return (panel.crop((offx, 0, panel.width, panel.height)),
                    int(wr - vw - p), int(panel_y - p))

        # 顶部 / 底部
        panel_x = int(round(wl + (ww - pw) * ratio))
        if self.vis <= 0.02:
            strip = self._get_strip()
            self.crop_off = (0, 0)
            sw, sh = strip.size
            x = int(panel_x + pw / 2 - sw / 2)
            y = wt_ if edge == EDGE_TOP else wb - sh
            return strip, int(x), int(y)
        panel = self._get_panel()
        if self.vis >= 0.999:
            self.crop_off = (0, 0)
            y = wt_ - p if edge == EDGE_TOP else wb - ph - p
            return panel, int(panel_x - p), int(y)
        vh = int(round(strip_px + (ph - strip_px) * self.vis))
        if edge == EDGE_TOP:
            self.crop_off = (0, 0)
            return (panel.crop((0, 0, panel.width, vh + p)),
                    int(panel_x - p), int(wt_ - p))
        offy = panel.height - (vh + p)
        self.crop_off = (0, offy)
        return (panel.crop((0, offy, panel.width, panel.height)),
                int(panel_x - p), int(wb - vh - p))

    # ------------------------------------------------------------ 动画
    def _animate(self, target: float) -> None:
        if abs(self.vis - target) < 0.005:
            self.vis = target
            self._present()
            return
        self.anim = (self.vis, target, time.monotonic(), 0.19)
        wc.user32.SetTimer(self.hwnd, TIMER_ANIM, 15, None)

    def _on_anim(self) -> None:
        if not self.anim:
            wc.user32.KillTimer(self.hwnd, TIMER_ANIM)
            return
        v0, v1, t0, dur = self.anim
        k = min(1.0, (time.monotonic() - t0) / dur) if dur > 0 else 1.0
        eased = 1 - (1 - k) ** 3
        self.vis = v0 + (v1 - v0) * eased
        self._present()
        if k >= 1.0:
            self.vis = v1
            self.anim = None
            wc.user32.KillTimer(self.hwnd, TIMER_ANIM)

    # ------------------------------------------------------------ 轮询
    def _on_poll(self) -> None:
        if self.dragging or self.menu_open or self.hidden:
            return
        if time.monotonic() < self._dock_cool:
            return
        cx, cy = wc.get_cursor_pos()
        if self.cfg.edge == EDGE_NONE:
            if self.vis < 1.0 and not self.anim:
                self.vis = 1.0
                self._present()
            return
        if not self.cfg.auto_hide:
            if self.vis < 1.0 and not self.anim:
                self._animate(1.0)
            return

        inside = self._in_window(cx, cy)
        if not inside and self.vis <= 0.02:
            inside = self._in_hotzone(cx, cy)
        if inside:
            self.mouse_away_since = None
            if self.vis < 0.999 and not self.anim:
                self._animate(1.0)
        else:
            if self.mouse_away_since is None:
                self.mouse_away_since = time.monotonic()
            if (self.vis > 0.001 and not self.anim
                    and time.monotonic() - self.mouse_away_since > 0.26):
                self._animate(0.0)

    def _in_window(self, x: int, y: int) -> bool:
        x0, y0, x1, y1 = self.rect
        return x0 <= x < x1 and y0 <= y < y1

    def _in_hotzone(self, x: int, y: int) -> bool:
        x0, y0, x1, y1 = self.rect
        horizontal = self.cfg.edge in (EDGE_TOP, EDGE_BOTTOM)
        if horizontal:
            return (x0 - 30 <= x <= x1 + 30) and (y0 - 8 <= y <= y1 + 8)
        return (x0 - 14 <= x <= x1 + 14) and (y0 - 30 <= y <= y1 + 30)

    # ------------------------------------------------------------ 数据
    def _on_data(self) -> None:
        snap, _ = self.sampler.read()
        changed = snap.ts != self._last_ts
        self._last_ts = snap.ts

        sens = self.sensors.read()
        sig = (
            round(sens.cpu.value) if sens.cpu else None,
            round(sens.gpu.value) if sens.gpu else None,
            round(sens.disk.value) if sens.disk else None,
            round(sens.board.value) if sens.board else None,
            round(sens.gpu_util) if sens.gpu_util is not None else None,
            round(sens.gpu_mem_used / 2 ** 30, 1) if sens.gpu_mem_used else None,
            tuple(sens.sources),
        )
        if sig != self._sens_sig:
            self._sens_sig = sig
            changed = True

        info = self.hardware.read()
        if info.collected != self._hw_ts:
            self._hw_ts = info.collected
            changed = True

        if not changed:
            return
        self.clock = time.strftime("%H:%M:%S")
        if self.hidden:
            return
        self._invalidate()
        self._present()

    # ------------------------------------------------------------ 命中测试
    def _image_coords(self, sx: int, sy: int) -> tuple[float, float]:
        return (sx - self.rect[0] + self.crop_off[0],
                sy - self.rect[1] + self.crop_off[1])

    def _hit_button(self, sx: int, sy: int) -> int:
        if self.vis < 0.85:
            return -1
        ix, iy = self._image_coords(sx, sy)
        rects = header_button_rects(self.scale, self._layout().horizontal)
        for i, kind in enumerate(("close", "menu")):
            x0, y0, x1, y1 = rects[kind]
            if x0 <= ix <= x1 and y0 <= iy <= y1:
                return i
        return -1

    def _in_header(self, sx: int, sy: int) -> bool:
        ix, iy = self._image_coords(sx, sy)
        x0, y0, x1, y1 = header_drag_rect(self.scale, self._layout().horizontal)
        return x0 <= ix <= x1 and y0 <= iy <= y1

    # ------------------------------------------------------------ 交互
    def _on_mouse_move(self) -> None:
        if self.dragging:
            cx, cy = wc.get_cursor_pos()
            nx = int(self._drag_win[0] + cx - self._drag_from[0])
            ny = int(self._drag_win[1] + cy - self._drag_from[1])
            if (nx, ny) != (self.cfg["float_x"], self.cfg["float_y"]):
                self.cfg["float_x"] = nx
                self.cfg["float_y"] = ny
                self._present()
            return
        cx, cy = wc.get_cursor_pos()
        btn = self._hit_button(cx, cy)
        if btn != self.hover_btn:
            self.hover_btn = btn
            self._invalidate()
            self._present()

    def _on_ldown(self) -> None:
        cx, cy = wc.get_cursor_pos()
        btn = self._hit_button(cx, cy)
        if btn == 0:
            self._hide_panel()
            return
        if btn == 1:
            self._show_menu()
            return
        if self.vis < 0.85:
            self._animate(1.0)
            return
        # 仅标题栏可拖动，避免误触
        if not self._in_header(cx, cy):
            return
        self._drag_orient = self.cfg.edge in (EDGE_TOP, EDGE_BOTTOM)
        if self.cfg.edge != EDGE_NONE:
            # 从贴边状态拖出：转为浮动，保持窗口位置不发生跳变
            self.cfg["float_x"] = int(self.rect[0])
            self.cfg["float_y"] = int(self.rect[1])
            self.cfg.edge = EDGE_NONE
            self.vis = 1.0
            self.anim = None
            self._invalidate()
        self.dragging = True
        self._drag_from = (cx, cy)
        self._drag_win = (self.rect[0], self.rect[1])
        self.mouse_away_since = None
        wc.user32.SetCapture(self.hwnd)
        self._present()

    def _on_lup(self) -> None:
        if not self.dragging:
            return
        self.dragging = False
        wc.user32.ReleaseCapture()
        cx, cy = wc.get_cursor_pos()
        wl, wt_, wr, wb = wc.monitor_work_area(cx, cy)
        lay = self._layout()
        p = lay.px(PAD)
        px0 = self.rect[0] + p
        py0 = self.rect[1] + p
        px1 = px0 + lay.panel_w
        py1 = py0 + lay.panel_h
        dists = {
            EDGE_LEFT: max(0, px0 - wl),
            EDGE_RIGHT: max(0, wr - px1),
            EDGE_TOP: max(0, py0 - wt_),
            EDGE_BOTTOM: max(0, wb - py1),
        }
        edge, dist = min(dists.items(), key=lambda kv: kv[1])
        if dist <= self.scale * 44:
            self._dock(edge, wl, wt_, wr, wb, (px0 + px1) // 2, (py0 + py1) // 2)
        else:
            self.cfg.edge = EDGE_NONE
            self.cfg["anchor_x"], self.cfg["anchor_y"] = cx, cy
            self._invalidate()
            self._present()
        self.cfg.save()
        # 拖到另一块屏幕后重算缩放（自动模式下不同像素密度表现不同）
        if self.cfg.ui_scale <= 0:
            self._apply_scale()

    def _dock(self, edge: str, wl: int, wt_: int, wr: int, wb: int,
              pcx: int, pcy: int) -> None:
        """吸附到指定边，并让面板位置沿边延续（以拖放时的面板中心为锚点）。"""
        self.cfg.edge = edge
        lay = self._layout()
        pw, ph = lay.panel_w, lay.panel_h
        ww, wh = wr - wl, wb - wt_
        if edge in (EDGE_LEFT, EDGE_RIGHT):
            ratio = (pcy - ph / 2 - wt_) / max(1, wh - ph)
            anchor_x = wl + 40 if edge == EDGE_LEFT else wr - 40
            anchor_y = wt_ + ph // 2
        else:
            ratio = (pcx - pw / 2 - wl) / max(1, ww - pw)
            anchor_x = wl + pw // 2
            anchor_y = wt_ + 40 if edge == EDGE_TOP else wb - 40
        self.cfg.ratio = min(1.0, max(0.0, ratio))
        self.cfg["anchor_x"], self.cfg["anchor_y"] = int(anchor_x), int(anchor_y)
        self.vis = 1.0
        self.anim = None
        self._invalidate()
        self._present()
        # 松手后立即滑入贴边，短暂冻结悬停，避免刚收起就被重新唤起
        self._dock_cool = time.monotonic() + 0.85
        if self.cfg.auto_hide:
            self.mouse_away_since = time.monotonic()
            self._animate(0.0)

    def _set_edge(self, edge: str) -> None:
        if edge == self.cfg.edge:
            return
        if edge == EDGE_NONE:
            self.cfg["float_x"], self.cfg["float_y"] = self.rect[0], self.rect[1]
        else:
            cx, cy = wc.get_cursor_pos()
            wl, wt_, wr, wb = wc.monitor_work_area(cx, cy)
            self.cfg["anchor_x"], self.cfg["anchor_y"] = cx, cy
            lay = Layout(self.scale, edge in (EDGE_TOP, EDGE_BOTTOM))
            if edge in (EDGE_LEFT, EDGE_RIGHT):
                ratio = (self.rect[1] + lay.px(PAD) - wt_) / max(
                    1, (wb - wt_) - lay.panel_h)
            else:
                ratio = (self.rect[0] + lay.px(PAD) - wl) / max(
                    1, (wr - wl) - lay.panel_w)
            self.cfg.ratio = min(1.0, max(0.0, ratio))
        self.cfg.edge = edge
        self.vis = 1.0
        self.anim = None
        self._dock_cool = time.monotonic() + 0.85
        self._invalidate()
        self._present()
        if edge != EDGE_NONE and self.cfg.auto_hide:
            self.mouse_away_since = time.monotonic()
            self._animate(0.0)
        self.cfg.save()

    def _hide_panel(self) -> None:
        self.hidden = True
        wc.user32.ShowWindow(self.hwnd, wc.SW_HIDE)
        if self.tray and self._first_hide_tip:
            self._first_hide_tip = False
            try:
                self.tray.notify("硬件监视已隐藏到托盘，双击托盘图标可重新显示。", "HW Monitor")
            except Exception:
                pass

    def _show_panel(self) -> None:
        self.hidden = False
        self.vis = 1.0
        self.anim = None
        self.mouse_away_since = None
        self._invalidate()
        self._present()

    # ------------------------------------------------------------ 菜单
    def _show_menu(self) -> None:
        u = wc.user32
        self.menu_open = True
        m = u.CreatePopupMenu()
        subs = []
        try:
            sub = u.CreatePopupMenu()
            subs.append(sub)
            for i, (label, e) in enumerate(EDGES):
                flags = wc.MF_STRING | (wc.MF_CHECKED if self.cfg.edge == e else 0)
                u.AppendMenuW(sub, flags, CMD_EDGE_BASE + i, label)
            u.AppendMenuW(m, wc.MF_POPUP, int(sub), "吸附位置")

            sub_s = u.CreatePopupMenu()
            subs.append(sub_s)
            auto_now = self.cfg.ui_scale <= 0
            auto_label = f"自动（{int(round(self._calc_scale() * 100))}%）"
            u.AppendMenuW(sub_s, wc.MF_STRING | (wc.MF_CHECKED if auto_now else 0),
                          CMD_SCALE_BASE, auto_label)
            u.AppendMenuW(sub_s, wc.MF_SEPARATOR, 0, None)
            for i, sc in enumerate(UI_SCALES):
                if sc <= 0:
                    continue
                flags = wc.MF_STRING | (
                    wc.MF_CHECKED
                    if (not auto_now and abs(self.cfg.ui_scale - sc) < 0.01) else 0)
                u.AppendMenuW(sub_s, flags, CMD_SCALE_BASE + i, f"{int(sc * 100)}%")
            u.AppendMenuW(m, wc.MF_POPUP, int(sub_s), "界面缩放")

            sub2 = u.CreatePopupMenu()
            subs.append(sub2)
            for i, iv in enumerate(INTERVALS):
                flags = wc.MF_STRING | (
                    wc.MF_CHECKED if abs(self.cfg.interval - iv) < 0.01 else 0)
                u.AppendMenuW(sub2, flags, CMD_INTERVAL_BASE + i, f"{iv:g} 秒")
            u.AppendMenuW(m, wc.MF_POPUP, int(sub2), "采样间隔")

            sub3 = u.CreatePopupMenu()
            subs.append(sub3)
            for i, op in enumerate(OPACITIES):
                flags = wc.MF_STRING | (
                    wc.MF_CHECKED if abs(self.cfg.opacity - op) < 0.01 else 0)
                u.AppendMenuW(sub3, flags, CMD_OPACITY_BASE + i, f"{int(op * 100)}%")
            u.AppendMenuW(m, wc.MF_POPUP, int(sub3), "不透明度")

            u.AppendMenuW(m, wc.MF_SEPARATOR, 0, None)
            u.AppendMenuW(m, wc.MF_STRING, CMD_COPY_HW, "复制硬件信息")
            u.AppendMenuW(m, wc.MF_STRING, CMD_REFRESH_HW, "重新扫描硬件")
            if not self.admin:
                u.AppendMenuW(m, wc.MF_STRING, CMD_ELEVATE,
                              "以管理员身份重启（完整传感器）")
            u.AppendMenuW(m, wc.MF_SEPARATOR, 0, None)
            u.AppendMenuW(m, wc.MF_STRING | (wc.MF_CHECKED if self.cfg.auto_hide else 0),
                          CMD_AUTOHIDE, "悬停展开（自动隐藏）")
            u.AppendMenuW(m, wc.MF_STRING | (wc.MF_CHECKED if autostart_enabled() else 0),
                          CMD_AUTOSTART, "开机自动启动")
            u.AppendMenuW(m, wc.MF_SEPARATOR, 0, None)
            u.AppendMenuW(m, wc.MF_STRING, CMD_HIDE, "隐藏到托盘")
            u.AppendMenuW(m, wc.MF_STRING, CMD_ABOUT, "关于")
            u.AppendMenuW(m, wc.MF_SEPARATOR, 0, None)
            u.AppendMenuW(m, wc.MF_STRING, CMD_QUIT, "退出")

            cx, cy = wc.get_cursor_pos()
            u.SetForegroundWindow(self.hwnd)
            cmd = u.TrackPopupMenu(
                m, wc.TPM_RETURNCMD | wc.TPM_RIGHTBUTTON | wc.TPM_LEFTALIGN | wc.TPM_TOPALIGN,
                cx, cy, 0, self.hwnd, None,
            )
            if cmd:
                self._handle_cmd(int(cmd))
        finally:
            for sub in subs:
                u.DestroyMenu(sub)
            u.DestroyMenu(m)
            u.PostMessageW(self.hwnd, 0, 0, 0)
            self.menu_open = False

    def _handle_cmd(self, cmd: int) -> None:
        if CMD_EDGE_BASE <= cmd < CMD_EDGE_BASE + len(EDGES):
            self._set_edge(EDGES[cmd - CMD_EDGE_BASE][1])
        elif cmd == CMD_SCALE_BASE:
            self.cfg.ui_scale = 0.0
            self._apply_scale(persist=True)
        elif CMD_SCALE_BASE < cmd <= CMD_SCALE_BASE + len(UI_SCALES) - 1:
            self.cfg.ui_scale = UI_SCALES[cmd - CMD_SCALE_BASE]
            self._apply_scale(persist=True)
        elif cmd == CMD_AUTOHIDE:
            self.cfg.auto_hide = not self.cfg.auto_hide
            if not self.cfg.auto_hide:
                self._animate(1.0)
            self.cfg.save()
        elif cmd == CMD_AUTOSTART:
            new_state = not autostart_enabled()
            set_autostart(new_state)
            self.cfg["autostart"] = new_state
            self.cfg.save()
        elif CMD_INTERVAL_BASE <= cmd < CMD_INTERVAL_BASE + len(INTERVALS):
            iv = INTERVALS[cmd - CMD_INTERVAL_BASE]
            self.cfg.interval = iv
            self.sampler.interval = iv
            self._invalidate()
            self._present()
            self.cfg.save()
        elif CMD_OPACITY_BASE <= cmd < CMD_OPACITY_BASE + len(OPACITIES):
            self.cfg.opacity = OPACITIES[cmd - CMD_OPACITY_BASE]
            self._present()
            self.cfg.save()
        elif cmd == CMD_HIDE:
            self._hide_panel()
        elif cmd == CMD_COPY_HW:
            self._copy_hardware()
        elif cmd == CMD_REFRESH_HW:
            self._refresh_hardware()
        elif cmd == CMD_ELEVATE:
            self._restart_elevated()
        elif cmd == CMD_ABOUT:
            self._show_about()
        elif cmd == CMD_QUIT:
            self._quit()

    def _show_about(self) -> None:
        sens = self.sensors.read()
        src = "、".join(sens.sources) if sens.sources else "未检测到"
        ctypes.windll.user32.MessageBoxW(
            None,
            "HwDock 硬件监视  2.1\n\n"
            "· 单页展示 CPU / 内存 / 显卡 / 磁盘 / 网络的占用、型号与温度\n"
            "· 面板与字号按屏幕像素密度自动缩放，2K / 4K 屏不再字小难辨\n"
            "· 拖动标题栏即可移动，靠近屏幕边缘松手会自动吸附并缩进\n"
            "· 吸附后只剩一条细条，鼠标移上去自动滑出\n\n"
            f"当前界面缩放：{int(round(self.scale * 100))}%"
            f"（{'自动' if self.cfg.ui_scale <= 0 else '手动'}）\n"
            f"当前温度来源：{src}\n"
            f"权限：{'管理员' if self.admin else '普通用户（部分传感器不可用）'}\n\n"
            "右键面板或托盘图标可调整吸附位置、界面缩放、采样间隔与开机自启。",
            "关于 HwDock", 0x00000040,
        )

    def _copy_hardware(self) -> None:
        text = hardware_summary(self.hardware.read(), self.sensors.read())
        ok = _set_clipboard(text)
        if self.tray:
            try:
                self.tray.notify("硬件信息已复制到剪贴板。" if ok else "复制失败。",
                                 "HW Monitor")
            except Exception:
                pass

    def _refresh_hardware(self) -> None:
        def work():
            try:
                self.hardware.refresh_once()
            except Exception:
                pass
            self._hw_ts = 0.0
            self._sens_sig = None
        threading.Thread(target=work, daemon=True).start()

    def _restart_elevated(self) -> None:
        try:
            if getattr(sys, "frozen", False):
                exe, params = sys.executable, ""
            else:
                exe = sys.executable
                params = f'"{os.path.abspath(sys.argv[0])}"'
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", exe, params, None, 1)
            if rc > 32:
                self._quit()
        except Exception:
            _log("提权失败:\n" + traceback.format_exc())

    def _quit(self) -> None:
        self.cfg.save()
        for hub in (self.sampler, self.sensors, self.hardware):
            try:
                hub.stop()
            except Exception:
                pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        wc.user32.PostQuitMessage(0)

    # ------------------------------------------------------------ 托盘
    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception:
            _log("pystray 不可用，跳过托盘")
            return

        def post(cmd):
            return lambda *a: wc.user32.PostMessageW(self.hwnd, WM_TRAY, cmd, 0)

        menu = pystray.Menu(
            pystray.MenuItem("显示面板", post(WM_SHOW), default=True),
            pystray.MenuItem("悬停展开（自动隐藏）", post(CMD_AUTOHIDE),
                             checked=lambda i: self.cfg.auto_hide),
            pystray.MenuItem("开机自动启动", post(CMD_AUTOSTART),
                             checked=lambda i: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", post(CMD_QUIT)),
        )
        self.tray = pystray.Icon("HwDock", tray_icon_image(64), "HwDock 硬件监视", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    # ------------------------------------------------------------ 窗口过程
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == wc.WM_TIMER:
                if wparam == TIMER_POLL:
                    self._on_poll()
                elif wparam == TIMER_DATA:
                    self._on_data()
                elif wparam == TIMER_ANIM:
                    self._on_anim()
                return 0
            if msg == wc.WM_MOUSEMOVE:
                self._on_mouse_move()
                return 0
            if msg == wc.WM_LBUTTONDOWN:
                self._on_ldown()
                return 0
            if msg == wc.WM_LBUTTONUP:
                self._on_lup()
                return 0
            if msg == wc.WM_RBUTTONUP:
                self._show_menu()
                return 0
            if msg == wc.WM_SETCURSOR:
                cx, cy = wc.get_cursor_pos()
                if self._hit_button(cx, cy) >= 0:
                    wc.user32.SetCursor(wc.cursor_handle(wc.IDC_HAND))
                    return 1
                if self.dragging:
                    wc.user32.SetCursor(wc.cursor_handle(wc.IDC_SIZEALL))
                    return 1
                if self._in_header(cx, cy):
                    wc.user32.SetCursor(wc.cursor_handle(wc.IDC_SIZEALL))
                    return 1
                return wc.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            if msg == WM_TRAY:
                self._handle_cmd(int(wparam))
                return 0
            if msg == WM_SHOW:
                self._show_panel()
                return 0
            if msg == wc.WM_DISPLAYCHANGE:
                if self._screen_dc:
                    wc.user32.ReleaseDC(None, self._screen_dc)
                    self._screen_dc = None
                self._apply_scale()
                return 0
            if msg == wc.WM_DPICHANGED:
                if self._screen_dc:
                    wc.user32.ReleaseDC(None, self._screen_dc)
                    self._screen_dc = None
                self._apply_scale()
                return 0
            if msg == wc.WM_CLOSE:
                self._quit()
                return 0
            if msg == wc.WM_DESTROY:
                wc.user32.PostQuitMessage(0)
                return 0
        except Exception:
            _log("wndproc 异常:\n" + traceback.format_exc())
        return wc.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
