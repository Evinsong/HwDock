"""开发辅助：直接把监视窗口内容抓成 PNG（不受其他窗口遮挡影响）。"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes as wt

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

u = ctypes.windll.user32
g = ctypes.windll.gdi32


class BMI(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def shot(path: str) -> tuple[int, int, int, int]:
    hwnd = u.FindWindowW("HwDock_Panel_v1", None)
    if not hwnd:
        raise SystemExit("未找到 HwDock 窗口")
    rect = RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    hdc = u.GetDC(None)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    old = g.SelectObject(mem, bmp)
    ok = u.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT
    bmi = BMI()
    bmi.biSize = ctypes.sizeof(BMI)
    bmi.biWidth = w
    bmi.biHeight = -h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    img.save(path)
    g.SelectObject(mem, old)
    g.DeleteObject(bmp)
    g.DeleteDC(mem)
    u.ReleaseDC(None, hdc)
    return rect.left, rect.top, w, h, ok


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "tools/build/win.png"
    if len(sys.argv) > 2:
        u.SetCursorPos(int(sys.argv[2]), int(sys.argv[3]))
        time.sleep(1.2)
    print(shot(out))
