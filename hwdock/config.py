"""配置持久化与开机自启。"""
from __future__ import annotations

import json
import os
import sys
import winreg

APP_NAME = "HwDock"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULTS = {
    "edge": "right",         # left / right / top / bottom / none
    "ratio": 0.5,            # 沿吸附边缘的相对位置
    "auto_hide": True,       # 吸附后自动收起，悬停展开
    "opacity": 1.0,          # 面板不透明度
    "interval": 1.0,         # 采样间隔（秒）
    "page": "mon",           # mon=实时监控 / hw=硬件详情
    "autostart": False,
    "ui_scale": 0.0,         # 界面缩放；0 = 跟随屏幕像素密度自动
    "float_x": None,
    "float_y": None,
    "anchor_x": None,
    "anchor_y": None,
}


def app_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def log_path() -> str:
    return os.path.join(app_dir(), "error.log")


class Config:
    def __init__(self, data: dict | None = None):
        self.data = dict(DEFAULTS)
        if data:
            self.data.update({k: v for k, v in data.items() if k in DEFAULTS})

    # ---------------------------------------------------------- 读写
    @classmethod
    def load(cls) -> "Config":
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                return cls(json.load(f))
        except Exception:
            return cls()

    def save(self) -> None:
        try:
            tmp = config_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, config_path())
        except Exception:
            pass

    # ---------------------------------------------------------- 属性
    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    @property
    def edge(self) -> str:
        return self["edge"]

    @edge.setter
    def edge(self, v: str):
        self["edge"] = v

    @property
    def ratio(self) -> float:
        return float(self["ratio"])

    @ratio.setter
    def ratio(self, v: float):
        self["ratio"] = max(0.0, min(1.0, float(v)))

    @property
    def auto_hide(self) -> bool:
        return bool(self["auto_hide"])

    @auto_hide.setter
    def auto_hide(self, v: bool):
        self["auto_hide"] = bool(v)

    @property
    def opacity(self) -> float:
        return max(0.5, min(1.0, float(self["opacity"])))

    @opacity.setter
    def opacity(self, v: float):
        self["opacity"] = max(0.5, min(1.0, float(v)))

    @property
    def page(self) -> str:
        return "hw" if self["page"] == "hw" else "mon"

    @page.setter
    def page(self, v: str):
        self["page"] = "hw" if v == "hw" else "mon"

    @property
    def interval(self) -> float:
        return float(self["interval"])

    @interval.setter
    def interval(self, v: float):
        self["interval"] = max(0.5, min(10.0, float(v)))

    @property
    def ui_scale(self) -> float:
        """0 表示自动（按屏幕像素密度推算）。"""
        try:
            return max(0.0, min(2.4, float(self["ui_scale"] or 0.0)))
        except Exception:
            return 0.0

    @ui_scale.setter
    def ui_scale(self, v: float):
        self["ui_scale"] = 0.0 if not v else round(max(0.7, min(2.4, float(v))), 3)


# ---------------------------------------------------------------- 开机自启
def exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = os.path.abspath(sys.argv[0])
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{script}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False
