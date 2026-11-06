"""HwDock 入口：单实例检测 + 启动窗口。"""
from __future__ import annotations

import ctypes
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hwdock import win32core as wc  # noqa: E402
from hwdock.app import CLASS_NAME, WM_SHOW, DockApp  # noqa: E402
from hwdock.config import log_path  # noqa: E402

MUTEX_NAME = "Global\\HwDock_SingleInstance_v1"
ERROR_ALREADY_EXISTS = 183


def already_running() -> bool:
    wc.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def main() -> int:
    if already_running():
        hwnd = wc.user32.FindWindowW(CLASS_NAME, None)
        if hwnd:
            wc.user32.PostMessageW(hwnd, WM_SHOW, 0, 0)
        return 0
    app = DockApp()
    app.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        detail = traceback.format_exc()
        try:
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(detail + "\n")
        except Exception:
            pass
        ctypes.windll.user32.MessageBoxW(
            None, f"HwDock 启动失败：\n\n{detail[-800:]}", "HwDock", 0x10
        )
        sys.exit(1)
