"""极简 WMI 客户端。

不依赖 pywin32 / wmi 包：直接用 ctypes 调用 COM 的 IWbemLocator 与
IWbemServices，一次连接、多次查询。用于读取主板 / BIOS / 内存条 /
显卡 / 硬盘 / ACPI 温区等只有 WMI 才提供的硬件信息。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from typing import Any, Iterable, Optional

ole32 = ctypes.WinDLL("ole32")
oleaut32 = ctypes.WinDLL("oleaut32")

HRESULT = ctypes.c_long
S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = 0x80010106
CLSCTX_INPROC_SERVER = 1

WBEM_FLAG_FORWARD_ONLY = 0x20
WBEM_FLAG_RETURN_IMMEDIATELY = 0x10
WBEM_INFINITE = 0xFFFFFFFF

CLSID_WBEM_LOCATOR = "{4590F811-1D3A-11D0-891F-00AA004B2E24}"
IID_IWBEM_LOCATOR = "{DC12A687-737F-11CF-884D-00AA004B2E24}"

# IWbemLocator / IWbemServices / IEnumWbemClassObject / IWbemClassObject
# 各方法的 vtable 下标（IUnknown 占 0..2）
VT_CONNECT_SERVER = 3
VT_EXEC_QUERY = 20
VT_NEXT = 4
VT_GET = 4   # IWbemClassObject::Get（vtable: QI/AddRef/Release 后第 2 个）
VT_RELEASE = 2

# VARIANT 类型
VT_EMPTY, VT_NULL, VT_I2, VT_I4, VT_R4, VT_R8, VT_BSTR = 0, 1, 2, 3, 4, 5, 8
VT_BOOL, VT_UI1, VT_UI2, VT_UI4, VT_I8, VT_UI8 = 11, 17, 18, 19, 20, 21
VT_ARRAY = 0x2000


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class VARIANT(ctypes.Structure):
    """只取前 16 字节：vt 头 + 一个指针宽度的值，足够读取标量。"""
    _fields_ = [
        ("vt", wt.WORD), ("r1", wt.WORD), ("r2", wt.WORD), ("r3", wt.WORD),
        ("value", ctypes.c_void_p),
    ]


def _bstr(text: Optional[str]):
    """分配真正的 BSTR（带长度前缀），再伪装成 LPWSTR 传入 COM。"""
    if text is None:
        return None
    oleaut32.SysAllocString.restype = ctypes.c_void_p
    oleaut32.SysAllocString.argtypes = [wt.LPCWSTR]
    ptr = oleaut32.SysAllocString(text)
    if not ptr:
        return None
    return ctypes.cast(ctypes.c_void_p(ptr), wt.LPWSTR)


def _free_bstr(p) -> None:
    if not p:
        return
    try:
        oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
        oleaut32.SysFreeString(ctypes.cast(p, ctypes.c_void_p))
    except Exception:
        pass


def _guid(text: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString.argtypes = [wt.LPCWSTR, ctypes.POINTER(GUID)]
    hr = ole32.CLSIDFromString(text, ctypes.byref(g))
    if hr != 0:
        raise OSError(f"CLSIDFromString 失败: {text}")
    return g


def _call(obj, index: int, restype, argtypes, *args):
    """按 vtable 下标调用 COM 方法。"""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = proto(vtbl[index])
    return fn(obj, *args)


def _release(obj) -> None:
    if not obj:
        return
    try:
        _call(obj, VT_RELEASE, ctypes.c_ulong, [])
    except Exception:
        pass


def _variant_to_py(buf, var: VARIANT) -> Any:
    """把 VARIANT 转成 Python 值；数组类型返回 None。"""
    vt = var.vt
    raw = ctypes.addressof(buf) + VARIANT.value.offset
    if vt in (VT_EMPTY, VT_NULL):
        return None
    if vt & VT_ARRAY:
        return None
    if vt == VT_BSTR:
        ptr = ctypes.c_void_p.from_address(raw).value
        return ctypes.wstring_at(ptr) if ptr else ""
    if vt == VT_I2:
        return ctypes.c_short.from_address(raw).value
    if vt == VT_I4:
        return ctypes.c_long.from_address(raw).value
    if vt == VT_UI1:
        return ctypes.c_ubyte.from_address(raw).value
    if vt == VT_UI2:
        return ctypes.c_ushort.from_address(raw).value
    if vt == VT_UI4:
        return ctypes.c_ulong.from_address(raw).value
    if vt == VT_I8:
        return ctypes.c_longlong.from_address(raw).value
    if vt == VT_UI8:
        return ctypes.c_ulonglong.from_address(raw).value
    if vt == VT_R4:
        return ctypes.c_float.from_address(raw).value
    if vt == VT_R8:
        return ctypes.c_double.from_address(raw).value
    if vt == VT_BOOL:
        return bool(ctypes.c_short.from_address(raw).value)
    return None


class WmiError(Exception):
    pass


_security_done = False
_com_ready = False


def _ensure_security() -> None:
    """设置进程级 COM 安全（模拟级别=Impersonate）。

    WMI 的本地调用需要它，否则枚举对象时返回 WBEM_E_ACCESS_DENIED。
    每个进程只能成功调用一次，重复调用返回 RPC_E_TOO_LATE，可忽略。
    """
    global _security_done
    if _security_done:
        return
    _security_done = True
    try:
        ole32.CoInitializeSecurity.argtypes = [
            ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
            wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
        ]
        ole32.CoInitializeSecurity.restype = HRESULT
        ole32.CoInitializeSecurity(None, -1, None, None, 0, 3, None, 0, None)
    except Exception:
        pass


class Wmi:
    """保持一个到指定命名空间的连接，反复查询开销极小。"""

    def __init__(self, namespace: str = r"ROOT\CIMV2"):
        self.namespace = namespace
        self._svc = None
        self._loc = None
        self._com = False
        self.ok = False
        try:
            self._open()
            self.ok = True
        except Exception:
            self.ok = False
            self.close()

    # -------------------------------------------------- 连接
    def _open(self) -> None:
        global _com_ready
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(GUID), ctypes.c_void_p, wt.DWORD,
            ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        ole32.CoCreateInstance.restype = HRESULT
        if not _com_ready:
            ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
            ole32.CoInitializeEx.restype = HRESULT
            hr = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
            if hr != S_OK and hr != S_FALSE and (hr & 0xFFFFFFFF) != RPC_E_CHANGED_MODE:
                raise WmiError(f"CoInitializeEx 0x{hr & 0xFFFFFFFF:08X}")
            _ensure_security()
            _com_ready = True
        loc = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid(CLSID_WBEM_LOCATOR)), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(IID_IWBEM_LOCATOR)), ctypes.byref(loc),
        )
        if (hr & 0xFFFFFFFF) != 0 or not loc.value:
            raise WmiError(f"CoCreateInstance 0x{hr & 0xFFFFFFFF:08X}")
        self._loc = loc

        svc = ctypes.c_void_p()
        b_ns = _bstr(self.namespace)
        try:
            hr = _call(
                loc, VT_CONNECT_SERVER, HRESULT,
                [wt.LPWSTR, wt.LPWSTR, wt.LPWSTR, wt.LPWSTR, ctypes.c_long,
                 wt.LPWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                b_ns, None, None, None, 0, None, None, ctypes.byref(svc),
            )
        finally:
            _free_bstr(b_ns)
        if (hr & 0xFFFFFFFF) != 0 or not svc.value:
            raise WmiError(f"ConnectServer 0x{hr & 0xFFFFFFFF:08X} ({self.namespace})")
        self._svc = svc

    def close(self) -> None:
        _release(self._svc)
        _release(self._loc)
        self._svc = self._loc = None
        self.ok = False
        # 注意：不调用 CoUninitialize。进程内 COM 一旦反初始化，
        # 之后重新初始化会丢失 CoInitializeSecurity 的设置，
        # 导致 WMI 枚举对象时报 WBEM_E_ACCESS_DENIED。

    # -------------------------------------------------- 查询
    def query(self, wql: str, fields: Iterable[str]) -> list[dict]:
        if not self._svc:
            return []
        fields = list(fields)
        enum = ctypes.c_void_p()
        b_lang = _bstr("WQL")
        b_wql = _bstr(wql)
        try:
            hr = _call(
                self._svc, VT_EXEC_QUERY, HRESULT,
                [wt.LPWSTR, wt.LPWSTR, ctypes.c_long, ctypes.c_void_p,
                 ctypes.POINTER(ctypes.c_void_p)],
                b_lang, b_wql,
                WBEM_FLAG_FORWARD_ONLY | WBEM_FLAG_RETURN_IMMEDIATELY,
                None, ctypes.byref(enum),
            )
        finally:
            _free_bstr(b_lang)
            _free_bstr(b_wql)
        if (hr & 0xFFFFFFFF) != 0 or not enum.value:
            return []

        rows: list[dict] = []
        try:
            while True:
                obj = ctypes.c_void_p()
                got = wt.ULONG(0)
                hr = _call(enum, VT_NEXT, HRESULT,
                           [ctypes.c_long, wt.ULONG, ctypes.POINTER(ctypes.c_void_p),
                            ctypes.POINTER(wt.ULONG)],
                           WBEM_INFINITE, 1, ctypes.byref(obj), ctypes.byref(got))
                if (hr & 0xFFFFFFFF) != 0 or got.value == 0 or not obj.value:
                    break
                try:
                    rows.append(self._read_object(obj, fields))
                finally:
                    _release(obj)
        finally:
            _release(enum)
        return rows

    @staticmethod
    def _read_object(obj, fields) -> dict:
        row: dict[str, Any] = {}
        for name in fields:
            buf = ctypes.create_string_buffer(ctypes.sizeof(VARIANT) + 8)
            var = VARIANT.from_buffer(buf)
            ctype = ctypes.c_long(0)
            flavor = ctypes.c_long(0)
            b_name = _bstr(name)
            try:
                hr = _call(obj, VT_GET, HRESULT,
                           [wt.LPWSTR, ctypes.c_long, ctypes.POINTER(VARIANT),
                            ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_long)],
                           b_name, 0, ctypes.byref(var), ctypes.byref(ctype),
                           ctypes.byref(flavor))
            finally:
                _free_bstr(b_name)
            if (hr & 0xFFFFFFFF) == 0:
                try:
                    row[name] = _variant_to_py(buf, var)
                except Exception:
                    row[name] = None
                try:
                    oleaut32.VariantClear.argtypes = [ctypes.POINTER(VARIANT)]
                    oleaut32.VariantClear(ctypes.byref(var))
                except Exception:
                    pass
            else:
                row[name] = None
        return row


def query(wql: str, fields: Iterable[str], namespace: str = r"ROOT\CIMV2") -> list[dict]:
    """一次性查询（内部自动连接/释放），失败返回空列表。"""
    w = Wmi(namespace)
    try:
        return w.query(wql, fields)
    finally:
        w.close()
