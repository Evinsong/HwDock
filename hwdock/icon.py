"""程序图标生成（托盘 / exe 图标）：小米风格橙色圆角方块 + 白色柱状。"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw


def _vgrad(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    w, h = size
    grad = Image.linear_gradient("L").resize((1, h)).resize((w, h))
    return Image.composite(Image.new("RGBA", (w, h), bottom),
                           Image.new("RGBA", (w, h), top), grad)


def make_icon(size: int = 256) -> Image.Image:
    s = size
    ss = max(2, min(6, 512 // max(1, size)))   # 小尺寸时多做超采样
    S = s * ss
    radius = int(S * 0.235)

    body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1),
                                           radius=radius, fill=255)
    body.paste(_vgrad((S, S), (255, 142, 66), (255, 88, 0)), (0, 0), mask)

    d = ImageDraw.Draw(body)
    bars = (0.44, 0.94, 0.66)
    bw = int(S * 0.132)
    gap = int(S * 0.076)
    total = bw * 3 + gap * 2
    x0 = (S - total) // 2
    base = int(S * 0.795)
    max_h = int(S * 0.545)
    r = bw // 2
    for i, frac in enumerate(bars):
        bx = x0 + i * (bw + gap)
        bh = max(int(bw * 1.15), int(max_h * frac))
        d.rounded_rectangle((bx, base - bh, bx + bw, base), radius=r,
                            fill=(255, 255, 255, 255))

    if ss > 1:
        body = body.resize((s, s), Image.LANCZOS)
    return body


def save_ico(path: str) -> None:
    img = make_icon(256)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    img.save(path, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                    (128, 128), (256, 256)])


def tray_icon_image(size: int = 64) -> Image.Image:
    return make_icon(size)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "assets/app.ico"
    save_ico(out)
    print("icon written:", out)
