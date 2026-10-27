"""Edge 浏览器定位、带调试端口启动、窗口枚举。

为什么必须用独立 profile：
Edge 已经在运行时，再执行 `msedge.exe --remote-debugging-port=9222` 不会
新开进程，而是把参数转交给已有实例，端口根本不会监听。所以必须用
`--user-data-dir` 指向一个新的独立 profile。代价是首次要在那个 profile 里
扫码登录一次视频号；之后登录态一直保留在 profile 目录中。

窗口相关：Edge 顶层窗口类名是 Chrome_WidgetWin_1，渲染子窗口类名是
Chrome_RenderWidgetHostHWND（与 Chrome 一致）。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import List, Optional, Tuple

# ----------------------------- 可执行文件探测 -----------------------------

_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
]

# 后台运行关键参数：
#   disable-background-timer-throttling / disable-renderer-backgrounding
#   —— 窗口被遮挡时 Chromium 会把定时器降频到 1 次/分钟，页面懒加载直接卡死
_BASE_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion,Translate,msEdgeIdentityFeatures",
    "--disable-sync",
    "--disable-extensions",
    "--remote-allow-origins=*",
]


def find_edge() -> Optional[Path]:
    for c in _EDGE_CANDIDATES:
        p = Path(c)
        if p.exists():
            return p
    # 兜底：从注册表找
    try:
        import winreg

        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                if val and Path(val).exists():
                    return Path(val)
            except OSError:
                continue
    except Exception:
        pass
    return None


def build_cmd(edge: Path, port: int, profile_dir: Path, start_url: str = "") -> List[str]:
    cmd = [
        str(edge),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
    ] + _BASE_FLAGS
    if start_url:
        cmd.append(start_url)
    return cmd


def launch(edge: Path, port: int, profile_dir: Path, start_url: str = "", minimized: bool = True) -> subprocess.Popen:
    """启动 Edge。minimized=True 时尽量不抢前台（窗口以最小化状态出现）。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(edge, port, profile_dir, start_url)
    si = None
    if minimized and os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        startupinfo=si,
    )


# ----------------------------- 端口探活 -----------------------------


def cdp_version(host: str = "127.0.0.1", port: int = 9222, timeout: float = 2.0) -> Optional[dict]:
    """探测调试端口。必须绕开系统代理，否则会被代理吞掉、表现为超时。"""
    url = f"http://{host}:{port}/json/version"
    try:
        with _NO_PROXY.open(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None


def cdp_alive(host: str = "127.0.0.1", port: int = 9222, timeout: float = 2.0) -> bool:
    return cdp_version(host, port, timeout) is not None


def wait_alive(host: str, port: int, timeout: float = 25.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cdp_alive(host, port, timeout=1.0):
            return True
        time.sleep(0.5)
    return False


# ----------------------------- 窗口枚举 -----------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

EDGE_WINDOW_CLASS = "Chrome_WidgetWin_1"
RENDER_WIDGET_CLASS = "Chrome_RenderWidgetHostHWND"

# 空代理：127.0.0.1 绝不能走系统代理
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def enum_windows() -> List[dict]:
    """枚举所有顶层可见窗口。返回 [{hwnd, title, cls, pid, visible}]"""
    out: List[dict] = []
    if _user32 is None:
        return out

    @WNDENUMPROC
    def _cb(hwnd, lparam):  # noqa: ARG001
        try:
            n = _user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            _user32.GetWindowTextW(hwnd, buf, n + 2)
            cbuf = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, cbuf, 256)
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append(
                {
                    "hwnd": int(hwnd),
                    "title": buf.value,
                    "cls": cbuf.value,
                    "pid": int(pid.value),
                    "visible": bool(_user32.IsWindowVisible(hwnd)),
                }
            )
        except Exception:
            pass
        return True

    _user32.EnumWindows(_cb, 0)
    return out


def find_edge_windows(title_kw: Tuple[str, ...] = ("edge", "视频号")) -> List[dict]:
    """找 Edge 主窗口：类名匹配 + 标题含关键字。"""
    res = []
    for w in enum_windows():
        if w["cls"] != EDGE_WINDOW_CLASS:
            continue
        t = (w["title"] or "").lower()
        if any(k.lower() in t for k in title_kw):
            res.append(w)
    return res


def find_render_child(hwnd: int) -> List[int]:
    """找指定窗口下的渲染子窗口（PrintWindow 与后台点击都作用在它上面）。"""
    if _user32 is None:
        return []
    kids: List[int] = []

    @WNDENUMPROC
    def _cb(child, lparam):  # noqa: ARG001
        cbuf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(child, cbuf, 256)
        if cbuf.value == RENDER_WIDGET_CLASS:
            kids.append(int(child))
        return True

    _user32.EnumChildWindows(wintypes.HWND(hwnd), _cb, 0)
    return kids


def is_minimized(hwnd: int) -> bool:
    return bool(_user32 and _user32.IsIconic(wintypes.HWND(hwnd)))


def get_window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    r = wintypes.RECT()
    if _user32 and _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    return (0, 0, 0, 0)
