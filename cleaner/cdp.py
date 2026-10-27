"""Chrome DevTools Protocol 客户端（对 Edge 完全等价）。

只用两条通道：
    HTTP  /json/list    列出可操作目标
    WS    <ws url>      指令通道

刻意不做的事（都会影响前台使用）：
    * 不调用 Page.bringToFront   —— 会把窗口切到前台
    * 不调用 /json/activate      —— 同上
    * 不启用 Input 之外的任何会占用焦点的域
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import websocket  # websocket-client
    from websocket import WebSocketTimeoutException
except Exception as exc:  # pragma: no cover
    websocket = None
    WebSocketTimeoutException = TimeoutError  # type: ignore
    _IMPORT_ERR = exc
else:
    _IMPORT_ERR = None


class CDPError(RuntimeError):
    pass


# 本机 HTTP 请求必须绕开系统代理：环境里存在 HTTP_PROXY 时，urllib 会把
# 127.0.0.1:9222 也送去代理，表现为"端口超时"，极难排查。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_get(url: str, timeout: float) -> str:
    with _NO_PROXY.open(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


@dataclass
class Target:
    id: str
    type: str
    title: str
    url: str
    ws_url: str

    def __str__(self) -> str:
        return f"[{self.type}] {self.title[:40]!r} {self.url[:70]}"


def list_targets(host: str = "127.0.0.1", port: int = 9222, timeout: float = 3.0) -> List[Target]:
    if websocket is None:
        raise CDPError(f"缺少 websocket-client 依赖，请先 pip install websocket-client（{_IMPORT_ERR}）")
    url = f"http://{host}:{port}/json/list"
    try:
        raw = _http_get(url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        raise CDPError(
            f"连不上 Edge 调试端口 {host}:{port} —— {e}\n"
            f"请先用 start-edge.bat 启动带调试端口的 Edge（独立 profile）。"
        ) from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise CDPError(f"调试端口返回了非 JSON 内容：{raw[:200]}") from e

    out: List[Target] = []
    for t in data:
        if not isinstance(t, dict):
            continue
        ws = t.get("webSocketDebuggerUrl") or ""
        if not ws:
            continue
        out.append(
            Target(
                id=t.get("id", ""),
                type=t.get("type", ""),
                title=t.get("title", "") or "",
                url=t.get("url", "") or "",
                ws_url=ws,
            )
        )
    return out


def new_tab(host: str, port: int, url: str, timeout: float = 5.0) -> Optional[Target]:
    """在调试实例里新开一个标签页（不会抢焦点）。"""
    import urllib.parse

    full = f"http://{host}:{port}/json/new?{urllib.parse.quote(url, safe='')}"
    try:
        req = urllib.request.Request(full, method="PUT")
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        ws = data.get("webSocketDebuggerUrl") or ""
        if not ws:
            return None
        return Target(data.get("id", ""), data.get("type", "page"), data.get("title", ""), data.get("url", ""), ws)
    except Exception:
        return None


def pick_target(targets: List[Target], match_url: str, prefer_types=("page",)) -> Optional[Target]:
    """按 URL 关键字挑目标，优先 page 类型。"""
    cands = [t for t in targets if t.type in prefer_types]
    if match_url:
        hit = [t for t in cands if match_url in (t.url or "")]
        if hit:
            # 多个命中时取标题最长的（通常是最完整的那个标签）
            return max(hit, key=lambda t: len(t.title))
    return None


class Session:
    """一个 target 的 WS 会话。"""

    def __init__(self, target: Target, timeout: float = 30.0):
        if websocket is None:
            raise CDPError(f"缺少 websocket-client 依赖（{_IMPORT_ERR}）")
        self.target = target
        self.timeout = timeout
        self._id = 0
        try:
            self._ws = websocket.create_connection(
                target.ws_url,
                timeout=timeout,
                suppress_origin=True,  # 不发 Origin 头，避免 403
                enable_multithread=True,
            )
        except Exception as e:
            raise CDPError(f"连接调试通道失败：{e}") from e

    # ---------------- 基础收发 ----------------
    def send(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        self._id += 1
        mid = self._id
        payload: Dict[str, Any] = {"id": mid, "method": method}
        if params:
            payload["params"] = params
        try:
            self._ws.send(json.dumps(payload))
        except Exception as e:
            raise CDPError(f"发送 {method} 失败：{e}") from e

        deadline = time.monotonic() + float(timeout or self.timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CDPError(f"{method} 等待响应超时（{timeout or self.timeout}s）")
            try:
                self._ws.settimeout(max(0.5, left))
                raw = self._ws.recv()
            except WebSocketTimeoutException as e:
                raise CDPError(f"{method} 等待响应超时") from e
            except Exception as e:
                raise CDPError(f"{method} 接收失败（连接可能已断开）：{e}") from e
            if not raw:
                raise CDPError(f"{method} 通道已关闭")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") != mid:
                continue  # 事件或其它响应，丢弃
            if "error" in msg:
                err = msg["error"]
                raise CDPError(f"{method} 返回错误：{err.get('message')} {err.get('data', '')}")
            return msg.get("result") or {}

    # ---------------- 常用封装 ----------------
    def evaluate(self, expression: str, return_by_value: bool = True, timeout: Optional[float] = None) -> Any:
        res = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": return_by_value,
                "awaitPromise": True,
                "userGesture": False,
                "includeCommandLineAPI": False,
            },
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            desc = (det.get("exception") or {}).get("description") or det.get("text") or "JS 异常"
            raise CDPError(f"页面脚本报错：{desc[:400]}")
        return (res.get("result") or {}).get("value")

    def screenshot(self, fmt: str = "png", timeout: float = 60.0) -> bytes:
        res = self.send("Page.captureScreenshot", {"format": fmt, "fromSurface": True, "captureBeyondViewport": False}, timeout=timeout)
        data = res.get("data") or ""
        if not data:
            raise CDPError("截图返回为空")
        return base64.b64decode(data)

    def navigate(self, url: str, wait: float = 2.0) -> None:
        self.send("Page.navigate", {"url": url}, timeout=30)
        if wait:
            time.sleep(wait)

    def current_url(self) -> str:
        try:
            return self.evaluate("location.href") or ""
        except CDPError:
            return ""

    def set_active(self) -> None:
        """让页面认为自己是活跃/聚焦的，但不真的抢占系统焦点。

        这是后台操作的关键：Edge 对后台标签会冻结渲染与输入，hover 菜单、
        懒加载都可能不响应。setFocusEmulationEnabled 只在页面内部生效。
        """
        for method, params in (
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ("Page.setWebLifecycleState", {"state": "active"}),
        ):
            try:
                self.send(method, params, timeout=5)
            except CDPError:
                pass  # 老版本 Edge 可能不支持，忽略

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Mouse:
    """通过 CDP 派发真实鼠标事件。不需要窗口在前台，也不会移动系统光标。"""

    def __init__(self, session: Session):
        self.s = session

    def move(self, x: float, y: float) -> None:
        self.s.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": round(x, 1), "y": round(y, 1), "button": "none", "buttons": 0, "clickCount": 0})

    def click(self, x: float, y: float, settle: float = 0.06) -> None:
        self.move(x, y)
        if settle:
            time.sleep(settle)
        self.s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": round(x, 1), "y": round(y, 1), "button": "left", "buttons": 1, "clickCount": 1, "pointerType": "mouse"})
        time.sleep(0.03)
        self.s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": round(x, 1), "y": round(y, 1), "button": "left", "buttons": 0, "clickCount": 1, "pointerType": "mouse"})

    def wheel(self, x: float, y: float, delta_y: int) -> None:
        self.s.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": round(x, 1), "y": round(y, 1), "deltaX": 0, "deltaY": int(delta_y), "button": "none", "buttons": 0})
