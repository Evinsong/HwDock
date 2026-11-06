"""Win32 API 绑定（ctypes），用于创建分层透明窗口、托盘、菜单、屏幕信息读取。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HICON = w.HANDLE

# ---------------------------------------------------------------- 常量
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_SETCURSOR = 0x0020
WM_APP = 0x8000
WM_DISPLAYCHANGE = 0x007E
WM_DPICHANGED = 0x02E0
WM_MOUSELEAVE = 0x02A3

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000

WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

DIB_RGB_COLORS = 0
BI_RGB = 0

IDC_ARROW = 32512
IDC_HAND = 32649
IDC_SIZEWE = 32644
IDC_SIZEALL = 32646

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MONITOR_DEFAULTTONEAREST = 2

TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_TOPALIGN = 0x0000

MF_STRING = 0x00000000
MF_POPUP = 0x00000010
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002

WS_EX_NOACTIVATE_ = WS_EX_NOACTIVATE

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001

# ---------------------------------------------------------------- 结构体
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, WPARAM, LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT),
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
        ("hIconSm", HICON),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD),
        ("biWidth", w.LONG),
        ("biHeight", w.LONG),
        ("biPlanes", w.WORD),
        ("biBitCount", w.WORD),
        ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD),
        ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("rcMonitor", w.RECT),
        ("rcWork", w.RECT),
        ("dwFlags", w.DWORD),
    ]


# ---------------------------------------------------------------- 原型声明
WNDPROC_PTR = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, WPARAM, LPARAM)

user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = w.ATOM
user32.CreateWindowExW.argtypes = [
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
]
user32.CreateWindowExW.restype = w.HWND
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = [w.HWND]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.UpdateLayeredWindow.argtypes = [
    w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE),
    w.HDC, ctypes.POINTER(w.POINT), w.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), w.DWORD,
]
user32.UpdateLayeredWindow.restype = w.BOOL
user32.SetWindowPos.argtypes = [
    w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT,
]
user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.GetMessageW.restype = w.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, WPARAM, LPARAM]
user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
user32.FindWindowW.restype = w.HWND
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.CreatePopupMenu.restype = w.HMENU
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.TrackPopupMenu.argtypes = [
    w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID,
]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [w.HMENU]
user32.LoadCursorW.argtypes = [w.HINSTANCE, w.LPCWSTR]
user32.LoadCursorW.restype = w.HANDLE
user32.SetCursor.argtypes = [w.HANDLE]
user32.SetCursor.restype = w.HANDLE
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.MonitorFromPoint.argtypes = [w.POINT, w.DWORD]
user32.MonitorFromPoint.restype = w.HMONITOR
user32.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
user32.MonitorFromWindow.restype = w.HMONITOR
user32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetDC.argtypes = [w.HWND]
user32.GetDC.restype = w.HDC
user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
user32.GetDpiForWindow.argtypes = [w.HWND]
user32.GetDpiForWindow.restype = w.UINT
user32.GetDesktopWindow.argtypes = []
user32.GetDesktopWindow.restype = w.HWND
gdi32.GetDeviceCaps.argtypes = [w.HDC, ctypes.c_int]
gdi32.GetDeviceCaps.restype = ctypes.c_int
user32.SetProcessDpiAwarenessContext.argtypes = [w.HANDLE]
user32.SetProcessDpiAwarenessContext.restype = w.BOOL
user32.SystemParametersInfoW.argtypes = [w.UINT, w.UINT, w.LPVOID, w.UINT]
user32.TrackMouseEvent.argtypes = [w.LPVOID]

gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.CreateDIBSection.argtypes = [
    w.HDC, ctypes.POINTER(BITMAPINFO), w.UINT,
    ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD,
]
gdi32.CreateDIBSection.restype = w.HBITMAP
gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
gdi32.DeleteDC.argtypes = [w.HDC]

kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HINSTANCE
kernel32.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
kernel32.CreateMutexW.restype = w.HANDLE


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("dwFlags", w.DWORD),
        ("hwndTrack", w.HWND),
        ("dwHoverTime", w.DWORD),
    ]


TME_LEAVE = 0x00000002


def set_dpi_aware() -> None:
    """开启 Per-Monitor DPI 感知，保证高 DPI 下不模糊。"""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def get_cursor_pos() -> tuple[int, int]:
    pt = w.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def cursor_handle(kind: int) -> int:
    return int(user32.LoadCursorW(None, ctypes.cast(kind, w.LPCWSTR)) or 0)


def monitor_work_area(x: int, y: int) -> tuple[int, int, int, int]:
    """返回包含指定点的显示器的可用工作区 (left, top, right, bottom)。"""
    pt = w.POINT(int(x), int(y))
    hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcWork
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    return (
        0,
        0,
        user32.GetSystemMetrics(SM_CXSCREEN),
        user32.GetSystemMetrics(SM_CYSCREEN),
    )


def monitor_dpi(x: int, y: int) -> int:
    """返回指定点所在显示器的有效 DPI（含系统缩放，96 为 100%）。"""
    pt = w.POINT(int(x), int(y))
    hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    try:
        shcore = ctypes.WinDLL("shcore")
        dx = w.UINT()
        dy = w.UINT()
        if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            if dx.value > 0:
                return int(dx.value)
    except Exception:
        pass
    try:
        dpi = int(user32.GetDpiForWindow(user32.GetDesktopWindow()) or 0)
        if dpi > 0:
            return dpi
    except Exception:
        pass
    hdc = user32.GetDC(None)
    try:
        dpi = int(gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
    finally:
        user32.ReleaseDC(None, hdc)
    return dpi or 96


def screen_rect(x: int, y: int) -> tuple[int, int, int, int]:
    pt = w.POINT(int(x), int(y))
    hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcMonitor
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    return (
        0,
        0,
        user32.GetSystemMetrics(SM_CXSCREEN),
        user32.GetSystemMetrics(SM_CYSCREEN),
    )


class Surface:
    """32 位 DIB 离屏画布，用于 UpdateLayeredWindow。"""

    def __init__(self) -> None:
        self.memdc = gdi32.CreateCompatibleDC(None)
        self.hbmp = None
        self.old = None
        self.bits = None
        self.width = 0
        self.height = 0

    def ensure(self, width: int, height: int) -> None:
        if (width, height) == (self.width, self.height) and self.hbmp:
            return
        self._release_bitmap()
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(
            self.memdc, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        self.bits = bits
        self.old = gdi32.SelectObject(self.memdc, self.hbmp)
        self.width = width
        self.height = height

    def _release_bitmap(self) -> None:
        if self.hbmp:
            if self.old:
                gdi32.SelectObject(self.memdc, self.old)
                self.old = None
            gdi32.DeleteObject(self.hbmp)
            self.hbmp = None
            self.bits = None

    def blit_bgra(self, data: bytes) -> None:
        if not self.bits:
            return
        ctypes.memmove(self.bits, data, len(data))

    def close(self) -> None:
        self._release_bitmap()
        if self.memdc:
            gdi32.DeleteDC(self.memdc)
            self.memdc = None
