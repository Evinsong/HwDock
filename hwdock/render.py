"""面板绘制：Pillow 离屏渲染 RGBA 位图，交给分层窗口显示。

设计取向：小米 HyperOS 风格的浅色卡片界面——
白色面板 + 浅灰卡片 + 大圆角 + 单色强调 + 克制的层级。
单页展示全部信息（CPU / 内存 / GPU / 磁盘 / 网络，含型号与温度），不再分页。
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Optional, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .metrics import HISTORY_LEN, Snapshot, format_bytes, format_rate

# ------------------------------------------------------------------ 主题（小米浅色）
PANEL_BG = (255, 255, 255)
SURF = (243, 245, 248)        # 卡片底
SURF_SOFT = (250, 251, 252)
HAIRLINE = (233, 236, 241)
TEXT = (23, 26, 31)
TEXT_SUB = (106, 113, 125)
TEXT_DIM = (144, 151, 163)
TEXT_MUTE = (178, 184, 194)
TRACK = (233, 236, 241)
RING_TRACK = (228, 232, 238)
HOVER = (17, 20, 26, 12)

ACCENT = {
    "cpu": (255, 105, 0),      # 小米橙
    "mem": (52, 130, 255),
    "gpu": (161, 110, 255),
    "disk": (0, 190, 110),
    "net": (0, 194, 209),
}
ACCENT_DIM = (150, 158, 170)   # 网络上传线

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
F_SEGOE = os.path.join(FONT_DIR, "segoeui.ttf")
F_SEGOE_B = os.path.join(FONT_DIR, "segoeuib.ttf")
F_YAHEI = os.path.join(FONT_DIR, "msyh.ttc")
F_YAHEI_B = os.path.join(FONT_DIR, "msyhbd.ttc")

# ------------------------------------------------------------------ 布局常量（96 DPI 基准）
PAD = 18          # 面板四周透明留白（容纳阴影）
RADIUS = 22
PANEL_W = 404
HEADER_H = 58
RING_H = 92
ROW_H = 92
ROW_GAP = 9
FOOTER_H = 26

# 横向（吸附顶部 / 底部）
H_TILE_W = 158
H_TILE_H = 186
H_TILE_GAP = 7

STRIP_W = 12      # 收起时的细条厚度
STRIP_LEN = 190   # 细条长度

EDGE_LEFT, EDGE_RIGHT, EDGE_TOP, EDGE_BOTTOM, EDGE_NONE = (
    "left", "right", "top", "bottom", "none"
)


# ------------------------------------------------------------------ 字体缓存
_font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}


def font(size: float, weight: str = "normal", cjk: bool = False) -> ImageFont.FreeTypeFont:
    px = max(8, int(round(size)))
    key = (px, weight, cjk)
    cached = _font_cache.get(key)
    if cached:
        return cached
    bold = weight in ("bold", "semibold")
    if cjk:
        path = F_YAHEI_B if bold else F_YAHEI
        if not os.path.exists(path):
            path = F_SEGOE_B if bold else F_SEGOE
    else:
        path = F_SEGOE_B if bold else F_SEGOE
    try:
        f = ImageFont.truetype(path, px)
    except Exception:
        try:
            f = ImageFont.truetype(F_SEGOE, px)
        except Exception:
            f = ImageFont.load_default()
    _font_cache[key] = f
    return f


# ------------------------------------------------------------------ 状态
@dataclass
class PanelState:
    snap: Snapshot
    hist: dict
    hostname: str = ""
    clock: str = ""
    edge: str = EDGE_NONE
    scale: float = 1.0
    hover_btn: int = -1      # 0=关闭 1=菜单
    interval: float = 1.0
    sensors: object = None
    hw: object = None
    admin: bool = False
    dragging: bool = False
    horizontal: bool = False


@dataclass
class MetricSpec:
    key: str
    name: str
    color: tuple
    value: str
    unit: str
    sub: str
    hist: Sequence[float]
    vmax: Optional[float] = 100.0
    bar: Optional[float] = None
    hist2: Optional[Sequence[float]] = None
    vmax_floor: float = 1.0
    smooth: int = 1
    temp: Optional[float] = None
    temp_label: str = ""
    ring: bool = True          # 是否出现在顶部环形区
    pct: Optional[float] = None


def temp_color(value: Optional[float]) -> tuple:
    """温度配色：正常绿 / 偏高琥珀 / 高温橙 / 危险红。"""
    if value is None:
        return TEXT_MUTE
    if value < 60:
        return (0, 178, 104)
    if value < 75:
        return (255, 149, 0)
    if value < 88:
        return (255, 106, 0)
    return (255, 59, 48)


def _pad_hist(vals: Sequence[float], n: int = HISTORY_LEN) -> list[float]:
    """补齐历史到固定长度，避免启动初期曲线被横向拉伸。"""
    if not vals:
        return []
    out = list(vals)
    if len(out) >= n:
        return out[-n:]
    return [out[0]] * (n - len(out)) + out


def _hardware_bits(hw) -> dict:
    """从硬件信息中提取每行需要的型号文本。"""
    out = {"cpu": "", "mem": "", "gpu": "", "disk": "", "net": "",
           "board": "", "os": ""}
    if hw is None:
        return out
    try:
        cpu = getattr(hw, "cpu_name", "") or ""
        out["cpu"] = cpu
        out["board"] = getattr(hw, "board", "") or ""
        out["os"] = getattr(hw, "os_name", "") or ""
        mods = getattr(hw, "mem_modules", []) or []
        if mods:
            m = mods[0]
            out["mem"] = " ".join(x for x in (
                f"{len(mods)}×{m.size_gb:.0f}GB",
                getattr(m, "kind", ""),
                f"{m.speed}" if getattr(m, "speed", 0) else "",
                getattr(m, "maker", ""),
            ) if x)
        gpus = getattr(hw, "gpus", []) or []
        if gpus:
            out["gpu"] = gpus[0].name or ""
        disks = getattr(hw, "disks", []) or []
        if disks:
            d = disks[0]
            bus = f" {d.bus}" if getattr(d, "bus", "") else ""
            out["disk"] = (d.model or "") + bus
        nets = getattr(hw, "nets", []) or []
        if nets:
            n = nets[0]
            ip = n.ips[0] if n.ips else ""
            out["net"] = " · ".join(x for x in (n.name, ip) if x)
    except Exception:
        pass
    return out


def build_metrics(state: PanelState) -> list[MetricSpec]:
    """组装五个指标（顺序与渲染一致）。"""
    s, h = state.snap, state.hist
    sens = state.sensors
    hw = _hardware_bits(state.hw)
    gpu_ok = s.gpu is not None
    cpu_t = getattr(sens, "cpu", None) if sens else None
    gpu_t = getattr(sens, "gpu", None) if sens else None
    disk_t = getattr(sens, "disk", None) if sens else None

    cpu_sub = " · ".join(x for x in (
        hw["cpu"],
        f"{s.threads} 线程" if s.threads else "",
        f"{s.cpu_freq:.2f} GHz" if s.cpu_freq else "",
    ) if x)
    mem_sub = " · ".join(x for x in (
        f"{format_bytes(s.mem_used)} / {format_bytes(s.mem_total)}",
        hw["mem"],
    ) if x)
    if gpu_ok:
        gpu_sub = " · ".join(x for x in (
            hw["gpu"] or s.gpu_name or "GPU",
        ) if x)
    else:
        gpu_sub = "GPU 性能计数器不可用"
    disk_sub = " · ".join(x for x in (
        hw["disk"],
        f"读 {format_rate(s.disk_read)}",
        f"写 {format_rate(s.disk_write)}",
    ) if x)
    net_sub = " · ".join(x for x in (
        hw["net"],
        f"↑ {format_rate(s.net_up)}",
    ) if x)

    return [
        MetricSpec(
            "cpu", "CPU", ACCENT["cpu"], f"{s.cpu:.0f}", "%", cpu_sub,
            _pad_hist(h.get("cpu", [])), 100.0, s.cpu, None, 1.0, 1,
            cpu_t.value if cpu_t else None,
            "温区" if (cpu_t and cpu_t.source == "ACPI") else "",
            True, s.cpu,
        ),
        MetricSpec(
            "mem", "内存", ACCENT["mem"], f"{s.mem_pct:.0f}", "%", mem_sub,
            _pad_hist(h.get("mem", [])), 100.0, s.mem_pct, None, 1.0, 1,
            None, "", True, s.mem_pct,
        ),
        MetricSpec(
            "gpu", "显卡", ACCENT["gpu"], f"{s.gpu:.0f}" if gpu_ok else "--",
            "%" if gpu_ok else "", gpu_sub,
            _pad_hist(h.get("gpu", [])), 100.0, s.gpu, None, 1.0, 1,
            gpu_t.value if gpu_t else None, "", True, s.gpu,
        ),
        MetricSpec(
            "disk", "磁盘", ACCENT["disk"], f"{s.disk_pct:.0f}", "%", disk_sub,
            _pad_hist(h.get("disk", [])), None, s.disk_pct, None, 2 * 1024 ** 2, 3,
            disk_t.value if disk_t else None, "", True, s.disk_pct,
        ),
        MetricSpec(
            "net", "网络", ACCENT["net"], format_rate(s.net_down), "", net_sub,
            _pad_hist(h.get("net_rx", [])), None, None,
            _pad_hist(h.get("net_tx", [])), 1 * 1024 ** 2, 3,
            None, "", False, None,
        ),
    ]


# ------------------------------------------------------------------ 布局
class Layout:
    """单页布局：竖向为「环形区 + 5 行卡片」，横向为「5 列卡片」。"""

    def __init__(self, scale: float = 1.0, horizontal: bool = False):
        self.s = scale
        self.horizontal = horizontal

    def px(self, v: float) -> int:
        return int(round(v * self.s))

    @property
    def panel_w(self) -> int:
        if self.horizontal:
            return self.px(5 * H_TILE_W + 4 * H_TILE_GAP + 24)
        return self.px(PANEL_W)

    @property
    def panel_h(self) -> int:
        if self.horizontal:
            return self.px(HEADER_H - 6 + H_TILE_H + 30)
        return self.px(HEADER_H + RING_H + 5 * ROW_H + 4 * ROW_GAP + ROW_GAP + 4 + FOOTER_H)

    @property
    def window_w(self) -> int:
        return self.panel_w + self.px(PAD) * 2

    @property
    def window_h(self) -> int:
        return self.panel_h + self.px(PAD) * 2

    def header_rect(self) -> tuple[float, float, float, float]:
        p = self.px(PAD)
        return (p, p, self.panel_w, self.px(HEADER_H))

    def rings_rect(self) -> tuple[int, int, int, int]:
        p = self.px(PAD)
        return (p + self.px(12), p + self.px(HEADER_H),
                self.panel_w - self.px(24), self.px(RING_H))

    def tile_rects(self, count: int = 5) -> list[tuple[int, int, int, int]]:
        p = self.px(PAD)
        gap = self.px(H_TILE_GAP)
        top = p + self.px(HEADER_H - 4)
        w = self.px(H_TILE_W)
        h = self.px(H_TILE_H)
        return [(p + self.px(12) + i * (w + gap), top, w, h) for i in range(count)]

    def row_rects(self, count: int = 5) -> list[tuple[int, int, int, int]]:
        p = self.px(PAD)
        gap = self.px(ROW_GAP)
        top = p + self.px(HEADER_H + RING_H + ROW_GAP)
        w = self.panel_w - self.px(24)
        h = self.px(ROW_H)
        return [(p + self.px(12), top + i * (h + gap), w, h) for i in range(count)]

    def footer_rect(self) -> tuple[float, float, float, float]:
        p = self.px(PAD)
        h = self.px(FOOTER_H)
        return (p + self.px(14), p + self.panel_h - h,
                self.panel_w - self.px(28), h)


# ------------------------------------------------------------------ 绘制工具
def _soft_shadow(size: tuple[int, int], panel_box, radius: int) -> Image.Image:
    w, h = size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    x0, y0, x1, y1 = panel_box
    d.rounded_rectangle((x0 + 1, y0 + 6, x1 + 1, y1 + 9), radius=radius, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(3.0, radius * 0.55)))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(mask.point(lambda v: int(v * 0.30)))
    return shadow


def _smooth(vals: Sequence[float], k: int) -> list[float]:
    if k <= 1 or len(vals) < k:
        return list(vals)
    out: list[float] = []
    for i in range(len(vals)):
        seg = vals[max(0, i - k + 1): i + 1]
        out.append(sum(seg) / len(seg))
    return out


def _spark_layer(w: int, h: int, values: Sequence[float], color: tuple,
                 vmax: Optional[float], alpha_fill: int = 46,
                 line_width: int = 2, draw_fill: bool = True,
                 vmax_floor: float = 1.0, smooth: int = 1,
                 total: int = 0) -> Image.Image:
    """独立图层上画折线 + 渐变面积。

    total > len(values) 时曲线贴右侧生长（左侧留空），
    避免启动初期少量采样被横向拉伸成一条假的平线。
    """
    layer = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
    if w < 4 or h < 4:
        return layer
    vals = _smooth(values, smooth) if values else [0.0]
    if len(vals) < 2:
        vals = [vals[0], vals[0]]
    n = len(vals)
    if vmax is None:
        peak = max(vals) if vals else 0.0
        vmax = max(peak * 1.3, vmax_floor)
    vmax = max(vmax, 1e-6)
    slots = max(total, n)
    span = max(1, slots - 1)
    x_start = (slots - n) / span * (w - 1)

    pts = []
    for i, v in enumerate(vals):
        x = x_start + i / span * (w - 1)
        y = (h - 3) - min(1.0, max(0.0, v / vmax)) * (h - 5)
        pts.append((x, y))

    d = ImageDraw.Draw(layer)
    if draw_fill and len(pts) >= 2:
        poly = pts + [(pts[-1][0], h), (pts[0][0], h)]
        d.polygon(poly, fill=color + (alpha_fill,))
        grad = Image.linear_gradient("L").resize((1, h))
        grad = Image.eval(grad, lambda v: max(0, 255 - int(v * 1.1)))
        mask = Image.new("L", (w, h), 0)
        mask.paste(grad.resize((w, h)), (0, 0))
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
        d = ImageDraw.Draw(layer)

    if len(pts) >= 2:
        d.line(pts, fill=color + (255,), width=line_width, joint="curve")
        cx, cy = pts[-1]
        r = max(1.7, line_width * 0.95)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (255,))
    return layer


def _ring_layer(size: int, frac: float, color: tuple, track: tuple,
                width: int, ss: int = 4) -> Image.Image:
    """超采样绘制环形进度，边缘平滑（小米式圆环）。"""
    S = max(8, size * ss)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lw = max(1, int(round(width * ss)))
    inset = lw / 2.0
    box = (inset, inset, S - 1 - inset, S - 1 - inset)
    d.arc(box, 0, 360, fill=track + (255,), width=lw)
    frac = max(0.0, min(1.0, frac))
    if frac > 0.004:
        end = -90 + 360 * frac
        d.arc(box, -90, end, fill=color + (255,), width=lw)
        r = (S - 1) / 2.0 - inset
        cx = cy = (S - 1) / 2.0
        rr = lw / 2.0
        for ang in (-90.0, end):
            a = math.radians(ang)
            px = cx + r * math.cos(a)
            py = cy + r * math.sin(a)
            d.ellipse((px - rr, py - rr, px + rr, py + rr), fill=color + (255,))
    return layer.resize((size, size), Image.LANCZOS)


def _fit_text(text: str, f: ImageFont.FreeTypeFont, max_w: float) -> str:
    if not text:
        return text
    if f.getlength(text) <= max_w:
        return text
    while text and f.getlength(text + "…") > max_w:
        text = text[:-1]
    return text + "…"


def _rounded_layer(size, radius, fill, outline=None) -> Image.Image:
    """圆角填充图层（用 mask 保证抗锯齿）。"""
    w, h = size
    ss = 4
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1), radius=radius * ss, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    layer = Image.new("RGBA", (w, h), fill)
    layer.putalpha(mask)
    if outline:
        edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(edge).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=radius, outline=outline, width=1)
        layer = Image.alpha_composite(layer, edge)
    return layer


# ------------------------------------------------------------------ 渲染器
class Renderer:
    def __init__(self, scale: float = 1.0):
        self.scale = max(0.8, min(3.0, scale))
        self._base_cache: dict = {}
        self._card_cache: dict = {}
        self._ring_cache: dict = {}

    # ---------------------------------------------------------- 静态底图
    def _card(self, w: int, h: int, radius: int, fill=SURF) -> Image.Image:
        key = ("card", w, h, radius, fill)
        cached = self._card_cache.get(key)
        if cached is not None:
            return cached
        card = _rounded_layer((w, h), radius, fill,
                              outline=(237, 240, 245))
        self._card_cache[key] = card
        return card

    def _ring(self, size: int, frac: float, color: tuple, track: tuple,
              width: int) -> Image.Image:
        """按 1% 量级缓存环形图，避免每秒重做超采样 + 缩放。"""
        level = int(round(max(0.0, min(1.0, frac)) * 100))
        key = (size, level, color)
        cached = self._ring_cache.get(key)
        if cached is None:
            cached = _ring_layer(size, level / 100.0, color, track, width)
            if len(self._ring_cache) > 360:
                self._ring_cache.clear()
            self._ring_cache[key] = cached
        return cached

    def _base_layer(self, lay: Layout) -> Image.Image:
        key = ("base", lay.horizontal, lay.panel_w, lay.panel_h)
        cached = self._base_cache.get(key)
        if cached is not None:
            return cached
        W, H = lay.window_w, lay.window_h
        p = lay.px(PAD)
        box = (p, p, p + lay.panel_w - 1, p + lay.panel_h - 1)
        radius = lay.px(RADIUS)

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        img = Image.alpha_composite(img, _soft_shadow((W, H), box, radius))

        body = _rounded_layer((lay.panel_w, lay.panel_h), radius, PANEL_BG,
                              outline=HAIRLINE)
        img.alpha_composite(body, (p, p))

        # 卡片底
        if lay.horizontal:
            rects = lay.tile_rects()
            radius_c = lay.px(18)
        else:
            rects = lay.row_rects()
            radius_c = lay.px(16)
        for rect in rects:
            x, y, w, h = rect
            img.alpha_composite(self._card(w, h, radius_c), (x, y))
        if not lay.horizontal:
            rx, ry, rw, rh = lay.rings_rect()
            img.alpha_composite(self._card(rw, rh, radius_c), (rx, ry))

        self._base_cache[key] = img
        return img

    # ---------------------------------------------------------- 主面板
    def render_panel(self, state: PanelState) -> Image.Image:
        lay = Layout(self.scale, state.horizontal)
        img = self._base_layer(lay).copy()
        d = ImageDraw.Draw(img)
        metrics = build_metrics(state)

        self._draw_header(img, d, lay, state)
        if lay.horizontal:
            for spec, rect in zip(metrics, lay.tile_rects()):
                self._draw_tile(img, d, lay, rect, spec)
        else:
            self._draw_rings(img, d, lay, state, metrics)
            for spec, rect in zip(metrics, lay.row_rects()):
                self._draw_row(img, d, lay, rect, spec)
        self._draw_footer(d, lay, state)
        return img

    # ---------------------------------------------------------- 头部
    def _draw_header(self, img: Image.Image, d: ImageDraw.ImageDraw, lay: Layout,
                     state: PanelState):
        hx, hy, hw, hh = lay.header_rect()
        pad = lay.px(14)

        # 应用图标：小米式圆角方块
        s = lay.px(30)
        ix, iy = hx + pad, hy + lay.px(13)
        badge = _rounded_layer((s, s), lay.px(9), ACCENT["cpu"])
        img.alpha_composite(badge, (ix, iy))
        bw = max(2, lay.px(2.6))
        for i, frac in enumerate((0.46, 0.82, 0.60)):
            bh = max(lay.px(4), int(lay.px(15) * frac))
            bx = ix + lay.px(8.4) + i * (bw + lay.px(2.6))
            by = iy + s - lay.px(9)
            d.rounded_rectangle((bx, by - bh, bx + bw, by),
                                radius=bw / 2, fill=(255, 255, 255, 245))

        tx = ix + s + lay.px(10)
        d.text((tx, hy + lay.px(11)), "硬件监视",
               font=font(lay.px(14), "semibold", cjk=True), fill=TEXT)
        meta = " · ".join(x for x in (
            state.hostname, _hardware_bits(state.hw)["os"]) if x)
        d.text((tx, hy + lay.px(33)), _fit_text(meta, font(lay.px(9.5), "normal", cjk=True),
                                                hw - tx - lay.px(120)),
               font=font(lay.px(9.5), "normal", cjk=True), fill=TEXT_DIM)

        # 右侧：时间 + 按钮
        btn = lay.px(26)
        gap_b = lay.px(4)
        right = hx + hw - lay.px(14)
        overlay = None
        for i, kind in enumerate(("close", "menu")):
            bx1 = right - (i + 1) * btn - i * gap_b
            box = (bx1, hy + lay.px(15), bx1 + btn, hy + lay.px(15) + btn)
            if state.hover_btn == i:
                if overlay is None:
                    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                ImageDraw.Draw(overlay).rounded_rectangle(
                    box, radius=lay.px(9), fill=HOVER)
            self._glyph(d, lay, box, kind,
                        TEXT if state.hover_btn == i else TEXT_SUB)
        if overlay is not None:
            img.alpha_composite(overlay)
            d = ImageDraw.Draw(img)
            for i, kind in enumerate(("close", "menu")):
                bx1 = right - (i + 1) * btn - i * gap_b
                box = (bx1, hy + lay.px(15), bx1 + btn, hy + lay.px(15) + btn)
                if state.hover_btn == i:
                    self._glyph(d, lay, box, kind, TEXT)

        # 时间（放在按钮左侧）
        if state.clock:
            d.text((right - 2 * btn - gap_b - lay.px(14), hy + lay.px(26)),
                   state.clock, font=font(lay.px(10), "normal"),
                   fill=TEXT_DIM, anchor="rm")

    def _glyph(self, d, lay: Layout, box, kind: str, color):
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        r = lay.px(4.8)
        wdt = max(1, round(lay.px(1.3)))
        if kind == "close":
            d.line((cx - r, cy - r, cx + r, cy + r), fill=color, width=wdt)
            d.line((cx - r, cy + r, cx + r, cy - r), fill=color, width=wdt)
        else:
            for dy in (-r * 0.82, 0, r * 0.82):
                d.rounded_rectangle(
                    (cx - r, cy + dy - lay.px(0.75), cx + r, cy + dy + lay.px(0.75)),
                    radius=lay.px(0.75), fill=color)

    # ---------------------------------------------------------- 顶部环形区
    def _draw_rings(self, img: Image.Image, d: ImageDraw.ImageDraw, lay: Layout,
                    state: PanelState, metrics: list[MetricSpec]):
        rx, ry, rw, rh = lay.rings_rect()
        rings = [m for m in metrics if m.ring][:4]

        n = max(1, len(rings))
        gap = lay.px(6)
        # 右侧留出网络速率块
        net_w = lay.px(78)
        area_w = rw - net_w - gap
        cell = area_w / n
        size = lay.px(54)
        top = ry + lay.px(4)

        for i, spec in enumerate(rings):
            cx = rx + cell * (i + 0.5)
            y = top
            frac = max(0.0, min(1.0, (spec.pct or 0) / 100.0))
            ring = self._ring(size, frac, spec.color, RING_TRACK, lay.px(5.5))
            img.alpha_composite(ring, (int(cx - size / 2), y))
            # 环心数值
            vf = font(lay.px(16), "bold")
            txt = f"{spec.pct:.0f}" if spec.pct is not None else "--"
            d.text((cx, y + size / 2 - lay.px(1.5)), txt, font=vf,
                   fill=TEXT, anchor="mm")
            d.text((cx, y + size / 2 + lay.px(9)),
                   "%" if spec.pct is not None else "",
                   font=font(lay.px(9), "normal"), fill=TEXT_MUTE, anchor="mm")
            # 名称
            d.text((cx, y + size + lay.px(8)), spec.name,
                   font=font(lay.px(9.5), "normal", cjk=True),
                   fill=TEXT_SUB, anchor="ma")

        # 网络速率块
        nx = rx + rw - net_w
        net = next((m for m in metrics if m.key == "net"), None)
        if net:
            s = state.snap
            by = top + lay.px(6)
            d.text((nx, by), "网络", font=font(lay.px(9), "normal", cjk=True),
                   fill=TEXT_MUTE)
            d.text((nx, by + lay.px(15)), "↓ " + format_rate(s.net_down),
                   font=font(lay.px(12), "semibold"), fill=TEXT)
            d.text((nx, by + lay.px(35)), "↑ " + format_rate(s.net_up),
                   font=font(lay.px(12), "semibold"), fill=ACCENT_DIM)

    # ---------------------------------------------------------- 指标行
    def _draw_row(self, img: Image.Image, d: ImageDraw.ImageDraw, lay: Layout,
                  rect, spec: MetricSpec):
        x, y, w, h = rect
        padx = lay.px(14)
        inner = w - padx * 2
        left_w = lay.px(132)

        # 名称 + 色点
        dot = lay.px(3)
        dcx, dcy = x + padx + dot, y + lay.px(17)
        d.ellipse((dcx - dot, dcy - dot, dcx + dot, dcy + dot),
                  fill=spec.color + (255,))
        d.text((dcx + dot + lay.px(6), y + lay.px(10)), spec.name,
               font=font(lay.px(10.5), "semibold", cjk=True), fill=TEXT_SUB)

        # 温度芯片（右上）
        if spec.temp is not None:
            self._chip(img, lay, x + w - padx, y + lay.px(9), spec.temp, "°C")

        # 主数值 + 单位（基线对齐）
        long_val = len(spec.value) > 6
        vf = font(lay.px(15 if long_val else 23), "bold")
        vy = y + lay.px(25 if not long_val else 29)
        d.text((x + padx, vy), spec.value, font=vf, fill=TEXT, anchor="la")
        if spec.unit:
            ascent, _ = vf.getmetrics()
            off = vf.getlength(spec.value) + lay.px(3)
            d.text((x + padx + off, vy + ascent), spec.unit,
                   font=font(lay.px(10)), fill=TEXT_MUTE, anchor="ls")

        # 曲线（右侧）
        cw = inner - left_w
        ch = lay.px(36)
        cx0 = x + padx + left_w
        cy0 = y + lay.px(24)
        if cw > 20 and ch > 8:
            if spec.hist2 is not None:
                l1 = _spark_layer(cw, ch, spec.hist2, ACCENT_DIM, spec.vmax,
                                  26, 2, True, spec.vmax_floor, spec.smooth, HISTORY_LEN)
                l2 = _spark_layer(cw, ch, spec.hist, spec.color, spec.vmax,
                                  24, 2, True, spec.vmax_floor, spec.smooth, HISTORY_LEN)
                img.alpha_composite(l1, (cx0, cy0))
                img.alpha_composite(l2, (cx0, cy0))
            else:
                layer = _spark_layer(cw, ch, spec.hist, spec.color, spec.vmax,
                                     40, 2, True, spec.vmax_floor, spec.smooth, HISTORY_LEN)
                img.alpha_composite(layer, (cx0, cy0))

        # 型号行（全宽，位于曲线下方）
        sub_f = font(lay.px(9), "normal", cjk=True)
        sub = _fit_text(spec.sub, sub_f, inner)
        d.text((x + padx, y + lay.px(62)), sub, font=sub_f, fill=TEXT_DIM)

        # 进度条
        if spec.bar is not None:
            bh = lay.px(4)
            by = y + h - lay.px(12)
            d.rounded_rectangle((x + padx, by, x + padx + inner, by + bh),
                                radius=bh / 2, fill=TRACK)
            frac = max(0.0, min(1.0, (spec.bar or 0) / 100.0))
            if frac > 0.004:
                fw = max(bh * 2.4, inner * frac)
                d.rounded_rectangle((x + padx, by, x + padx + fw, by + bh),
                                    radius=bh / 2, fill=spec.color + (255,))
        else:
            # 无百分比（网络）：画一条上下行对照的细线，保持行底视觉重量
            bh = lay.px(2)
            by = y + h - lay.px(11)
            d.rounded_rectangle((x + padx, by, x + padx + inner, by + bh),
                                radius=bh / 2, fill=(238, 241, 246))

    def _chip(self, img: Image.Image, lay: Layout, right: int, top: int,
              value: float, suffix: str = "°C"):
        """小米式温度胶囊：淡色底 + 彩色文字，画在独立图层避免半透明叠色失真。"""
        color = temp_color(value)
        text = f"{value:.0f}{suffix}"
        f = font(lay.px(9), "semibold")
        pad = lay.px(6)
        tw = f.getlength(text)
        w = int(tw + pad * 2)
        h = lay.px(17)
        chip = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        cd = ImageDraw.Draw(chip)
        cd.rounded_rectangle((0, 0, w - 1, h - 1), radius=h / 2,
                             fill=color + (34,))
        cd.text((w / 2, h / 2 - lay.px(0.5)), text, font=f,
                fill=color + (255,), anchor="mm")
        img.alpha_composite(chip, (int(right - w), int(top)))

    # ---------------------------------------------------------- 横向卡片
    def _draw_tile(self, img: Image.Image, d: ImageDraw.ImageDraw, lay: Layout,
                   rect, spec: MetricSpec):
        x, y, w, h = rect
        padx = lay.px(13)
        inner = w - padx * 2

        # 标题 + 温度
        d.text((x + padx, y + lay.px(11)), spec.name,
               font=font(lay.px(10), "semibold", cjk=True), fill=TEXT_SUB)
        if spec.temp is not None:
            self._chip(img, lay, x + w - padx, y + lay.px(9), spec.temp, "°")

        # 环形
        size = lay.px(58)
        frac = max(0.0, min(1.0, (spec.pct or 0) / 100.0))
        rx = x + (w - size) / 2
        ry = y + lay.px(31)
        if spec.pct is not None:
            img.alpha_composite(self._ring(size, frac, spec.color, RING_TRACK, lay.px(5.5)),
                                (int(rx), int(ry)))
            d.text((rx + size / 2, ry + size / 2 - lay.px(1)),
                   f"{spec.pct:.0f}", font=font(lay.px(15), "bold"),
                   fill=TEXT, anchor="mm")
            d.text((rx + size / 2, ry + size / 2 + lay.px(10)), "%",
                   font=font(lay.px(9), "normal"), fill=TEXT_MUTE, anchor="mm")
        else:
            d.text((x + w / 2, ry + size / 2), spec.value,
                   font=font(lay.px(13), "bold"), fill=TEXT, anchor="mm")

        # 型号：按「·」拆成最多两行，逐行截断（不做折行续接）
        parts = [p.strip() for p in spec.sub.split(" · ") if p.strip()]
        sub_f = font(lay.px(9.5), "normal", cjk=True)
        if parts:
            d.text((x + padx, y + lay.px(97)),
                   _fit_text(parts[0], sub_f, inner), font=sub_f, fill=TEXT_DIM)
        if len(parts) > 1:
            d.text((x + padx, y + lay.px(110)),
                   _fit_text(" · ".join(parts[1:]), sub_f, inner),
                   font=sub_f, fill=TEXT_DIM)

        # 曲线
        cw = inner
        ch = lay.px(32)
        cy0 = y + lay.px(126)
        if spec.hist2 is not None:
            img.alpha_composite(
                _spark_layer(cw, ch, spec.hist2, ACCENT_DIM, spec.vmax, 26, 2,
                             True, spec.vmax_floor, spec.smooth, HISTORY_LEN), (x + padx, cy0))
            img.alpha_composite(
                _spark_layer(cw, ch, spec.hist, spec.color, spec.vmax, 24, 2,
                             True, spec.vmax_floor, spec.smooth, HISTORY_LEN), (x + padx, cy0))
        else:
            img.alpha_composite(
                _spark_layer(cw, ch, spec.hist, spec.color, spec.vmax, 40, 2,
                             True, spec.vmax_floor, spec.smooth, HISTORY_LEN), (x + padx, cy0))

        if spec.bar is not None:
            bh = lay.px(4)
            by = y + h - lay.px(14)
            d.rounded_rectangle((x + padx, by, x + padx + cw, by + bh),
                                radius=bh / 2, fill=TRACK)
            fw = max(bh * 2.4, cw * frac)
            if frac > 0.004:
                d.rounded_rectangle((x + padx, by, x + padx + fw, by + bh),
                                    radius=bh / 2, fill=spec.color + (255,))

    # ---------------------------------------------------------- 页脚
    def _draw_footer(self, d: ImageDraw.ImageDraw, lay: Layout, state: PanelState):
        fx, fy, fw, fh = lay.footer_rect()
        f = font(lay.px(9.5), "normal", cjk=True)
        hw = _hardware_bits(state.hw)
        sens = state.sensors
        sources = getattr(sens, "sources", None) or []
        bits = [x for x in (hw["board"], state.hostname) if x]
        left = " · ".join(bits) if bits else state.hostname
        if sources:
            left = f"{left} · 传感器 {'/'.join(str(s) for s in sources[:3])}"
        elif not state.admin:
            left = f"{left} · 非管理员运行"
        left = _fit_text(left, f, fw * 0.8)
        d.text((fx, fy + lay.px(7)), left, font=f, fill=TEXT_MUTE, anchor="la")

        iv = state.interval
        iv_txt = f"{iv:g}"
        d.text((fx + fw, fy + lay.px(7)), f"{iv_txt}s 刷新", font=f,
               fill=TEXT_MUTE, anchor="ra")

    # ---------------------------------------------------------- 收起细条
    def render_strip(self, state: PanelState) -> Image.Image:
        s = self.scale
        vertical = state.edge in (EDGE_LEFT, EDGE_RIGHT)
        thickness = int(round(STRIP_W * s))
        length = int(round(STRIP_LEN * s))
        W, H = (thickness, length) if vertical else (length, thickness)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # 阴影（细条两侧柔和）
        sh = Image.new("L", (W, H), 0)
        ImageDraw.Draw(sh).rounded_rectangle((0, 1, W - 1, H - 2),
                                             radius=thickness / 2, fill=255)
        sh = sh.filter(ImageFilter.GaussianBlur(2.2))
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        glow.putalpha(sh.point(lambda v: int(v * 0.22)))
        img = Image.alpha_composite(img, glow)
        d = ImageDraw.Draw(img)

        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=thickness / 2,
                            fill=(255, 255, 255, 250),
                            outline=HAIRLINE, width=1)

        specs = [m for m in build_metrics(state) if m.pct is not None][:4]
        n = max(1, len(specs))
        pad = max(1, int(round(thickness * 0.26)))
        inner_w = thickness - pad * 2
        inner_len = length - pad * 2
        seg = (inner_len - (n - 1) * pad) / n

        for i, spec in enumerate(specs):
            frac = max(0.0, min(1.0, (spec.pct or 0) / 100.0))
            if vertical:
                top = pad + i * (seg + pad)
                d.rounded_rectangle((pad, top, pad + inner_w, top + seg),
                                    radius=inner_w / 2, fill=TRACK)
                fill = max(inner_w, seg * frac)
                d.rounded_rectangle((pad, top + seg - fill, pad + inner_w, top + seg),
                                    radius=inner_w / 2, fill=spec.color + (255,))
            else:
                left = pad + i * (seg + pad)
                d.rounded_rectangle((left, pad, left + seg, pad + inner_w),
                                    radius=inner_w / 2, fill=TRACK)
                fill = max(inner_w, seg * frac)
                d.rounded_rectangle((left, pad, left + fill, pad + inner_w),
                                    radius=inner_w / 2, fill=spec.color + (255,))
        return img


# ------------------------------------------------------------------ 命中测试辅助
def header_button_rects(scale: float, horizontal: bool = False) -> dict:
    """头部按钮在图像坐标系中的矩形（0=关闭, 1=菜单）。"""
    lay = Layout(scale, horizontal)
    hx, hy, hw, hh = lay.header_rect()
    btn = lay.px(26)
    gap_b = lay.px(4)
    right = hx + hw - lay.px(14)
    out = {}
    for i, kind in enumerate(("close", "menu")):
        bx1 = right - (i + 1) * btn - i * gap_b
        out[kind] = (bx1, hy + lay.px(15), bx1 + btn, hy + lay.px(15) + btn)
        out[i] = out[kind]
    return out


def header_drag_rect(scale: float, horizontal: bool = False):
    lay = Layout(scale, horizontal)
    hx, hy, hw, hh = lay.header_rect()
    return (hx, hy, hx + hw, hy + lay.px(58))


def panel_box_in_image(scale: float, horizontal: bool = False):
    lay = Layout(scale, horizontal)
    p = lay.px(PAD)
    return (p, p, p + lay.panel_w, p + lay.panel_h)
