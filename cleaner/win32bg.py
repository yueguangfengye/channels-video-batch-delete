"""Win32 后台窗口能力：PrintWindow 截图 + 句柄消息点击 + 窗口状态检查。

⚠️ 重要前提（决定它在本项目里的角色）
Edge 是 Chromium，渲染走独立进程 + DirectComposition。实测：
    * PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT) 通常能抓到画面，可用于
      后台取证、模板匹配；窗口被别的窗口遮住也没关系。
    * PostMessage(WM_LBUTTONDOWN) 到渲染子窗口，绝大多数版本 Edge 不响应
      （渲染器只认合成器注入的输入）。所以后台点击**不能**指望它。

因此本项目里：
    CDP 派发鼠标事件 = 唯一的点击通道（不抢焦点、不动光标）
    Win32 PrintWindow = 后台截图的备用取证通道
    这里的 post_click() 保留为「最后手段」，默认关闭，且调用方必须校验结果。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Optional, Tuple

import numpy as np

from . import edge

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

_IS_WIN = os.name == "nt"
_user32 = ctypes.WinDLL("user32", use_last_error=True) if _IS_WIN else None
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True) if _IS_WIN else None
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True) if _IS_WIN else None


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _configure() -> None:
    if not _IS_WIN:
        return
    _user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _user32.PrintWindow.restype = wintypes.BOOL
    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _gdi32.SelectObject.restype = wintypes.HGDIOBJ
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT]
    _gdi32.GetDIBits.restype = ctypes.c_int


if _IS_WIN:
    try:
        _configure()
    except Exception:
        pass


def capture_window(hwnd: int, flags: int = PW_RENDERFULLCONTENT) -> Optional[np.ndarray]:
    """后台抓取整个窗口的位图（BGR）。失败返回 None。"""
    if not _IS_WIN:
        return None
    left, top, right, bottom = edge.get_window_rect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    hdc = None
    mfc = None
    bmp = None
    old = None
    try:
        hdc = _user32.GetWindowDC(wintypes.HWND(hwnd))
        if not hdc:
            return None
        mfc = _gdi32.CreateCompatibleDC(hdc)
        bmp = _gdi32.CreateCompatibleBitmap(hdc, w, h)
        if not mfc or not bmp:
            return None
        old = _gdi32.SelectObject(mfc, bmp)
        _user32.PrintWindow(wintypes.HWND(hwnd), mfc, flags)  # 返回值不可信

        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h  # 负值 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        lines = _gdi32.GetDIBits(mfc, bmp, 0, h, ctypes.c_void_p(ctypes.addressof(buf)), ctypes.byref(bi), 0)
        if lines == 0:
            return None
        arr = np.frombuffer(buf.raw, dtype=np.uint8)
        if arr.size != w * h * 4:
            return None
        arr = arr.reshape((h, w, 4))
        return arr[:, :, :3].copy()
    except Exception:
        return None
    finally:
        try:
            if old and mfc:
                _gdi32.SelectObject(mfc, old)
            if bmp:
                _gdi32.DeleteObject(bmp)
            if mfc:
                _gdi32.DeleteDC(mfc)
            if hdc:
                _user32.ReleaseDC(wintypes.HWND(hwnd), hdc)
        except Exception:
            pass


def is_cloaked(hwnd: int) -> bool:
    """窗口是否被 DWM 隐藏（例如在别的虚拟桌面）。"""
    if not _IS_WIN or _dwmapi is None:
        return False
    DWMWA_CLOAKED = 14
    val = wintypes.DWORD(0)
    try:
        hr = _dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and val.value != 0
    except Exception:
        return False


def window_state(hwnd: int) -> dict:
    return {
        "hwnd": hwnd,
        "minimized": edge.is_minimized(hwnd),
        "cloaked": is_cloaked(hwnd),
        "rect": edge.get_window_rect(hwnd),
    }


def post_click(hwnd: int, x: int, y: int, settle: float = 0.05) -> bool:
    """向窗口句柄投递点击消息（客户区坐标）。

    对 Chromium **大概率无效**，仅作为最后手段保留；调用方必须校验点击结果。
    """
    if not _IS_WIN:
        return False
    lp = ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)
    try:
        _user32.PostMessageW(wintypes.HWND(hwnd), WM_MOUSEMOVE, 0, lp)
        _user32.PostMessageW(wintypes.HWND(hwnd), WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(settle)
        _user32.PostMessageW(wintypes.HWND(hwnd), WM_LBUTTONUP, 0, lp)
        return True
    except Exception:
        return False


def capture_evidence(title_kw: Tuple[str, ...] = ("视频号", "Edge")) -> Optional[np.ndarray]:
    """找一个 Edge 窗口做后台截图（用于日志取证，不影响前台）。"""
    wins = edge.find_edge_windows(title_kw)
    if not wins:
        wins = edge.find_edge_windows(("edge",))
    for w in wins:
        img = capture_window(w["hwnd"])
        if img is not None and float(img.std()) > 2.0:
            return img
    return None
