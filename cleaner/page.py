"""把 DOM 探针包装成 Python 对象。

这是「后台操作」的落点：
    - 所有定位都通过 DOM 完成（不依赖像素位置）
    - 所有点击都通过 CDP 的 Input.dispatchMouseEvent 派发（不移动系统光标、
      不需要窗口在前台、不需要任何焦点）
    - 所有截图都通过 Page.captureScreenshot 抓（窗口被遮挡也能抓）
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import cdp, dom


@dataclass
class Item:
    """列表 / 垃圾桶里的一个视频条目。"""

    key: str
    title: str
    time_text: str
    date_str: str
    in_range: bool
    card_id: str
    rect: Dict[str, int]
    action_id: Optional[str] = None
    action_text: Optional[str] = None
    phase: str = "list"

    def short(self, n: int = 34) -> str:
        t = (self.title or "(无标题)").replace("\n", " ")
        return t if len(t) <= n else t[: n - 1] + "…"


class EdgePage:
    def __init__(self, session: cdp.Session, cfg, rec):
        self.s = session
        self.cfg = cfg
        self.r = rec
        self.mouse = cdp.Mouse(session)
        self._delete_texts = list(cfg.get("selectors.delete_texts", ["删除"]))
        self._more_texts = list(cfg.get("selectors.more_texts", ["更多", "…", "..."]))
        self._confirm_texts = list(cfg.get("selectors.confirm_texts", ["确定", "确认删除", "删除", "确认"]))
        self._roots = list(cfg.get("selectors.list_root", [""]))
        self.page_wait = float(cfg.get("limits.page_wait", 1.6))

    # ---------------- 底层 ----------------
    def js(self, body: str, timeout: float = 20.0) -> Any:
        return self.s.evaluate(dom.wrap(body), timeout=timeout)

    def jcall(self, fn: str, *args: Any) -> Any:
        payload = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        return self.js("W." + fn + "(" + payload + ")")

    def info(self) -> dict:
        try:
            return self.jcall("pageInfo", 1500) or {}
        except cdp.CDPError:
            return {}

    def quick(self, words: Optional[List[str]] = None) -> dict:
        """高频检查用：只做关键词命中判断，不拉全文、不做布局计算。

        视频号的内容在 shadow DOM 里，textContent 会把 <style> 的 CSS 源码也带上，
        所以不能拿全文做子串匹配的主依据——这里只返回命中了哪些关键词。
        """
        words = words or []
        try:
            return self.jcall("hits", list(words)) or {}
        except cdp.CDPError:
            return {}

    def hits(self, words: List[str]) -> List[str]:
        return list(self.quick(words).get("hits") or [])

    def press_escape(self) -> None:
        """关掉可能残留的浮层菜单（点空白处容易点到别的东西，用 ESC 更安全）。"""
        for typ in ("keyDown", "keyUp"):
            try:
                self.s.send(
                    "Input.dispatchKeyEvent",
                    {"type": typ, "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27},
                    timeout=5,
                )
            except cdp.CDPError:
                return

    def url(self) -> str:
        try:
            return self.s.evaluate("location.href") or ""
        except cdp.CDPError:
            return ""

    def sleep(self, s: float) -> None:
        time.sleep(max(0.0, s))

    # ---------------- 列表扫描 ----------------
    def scan_list(self) -> List[dict]:
        res = self.jcall(
            "scanList",
            {
                "roots": self._roots,
                "maxUp": int(self.cfg.get("selectors.card_max_up", 8)),
                "minW": int(self.cfg.get("selectors.card_min_width", 240)),
                "minH": int(self.cfg.get("selectors.card_min_height", 56)),
                "maxH": int(self.cfg.get("selectors.card_max_height", 420)),
                "deleteTexts": self._delete_texts,
                "moreTexts": self._more_texts,
            },
        )
        if not res or not isinstance(res, dict):
            return []
        return res.get("items") or []

    def scroll(self, ratio: float = 0.85) -> dict:
        try:
            return self.jcall("scrollDown", ratio) or {}
        except cdp.CDPError:
            return {}

    # ---------------- 分页 ----------------
    def pager(self) -> dict:
        try:
            return self.jcall("pager") or {"found": False}
        except cdp.CDPError:
            return {"found": False}

    def click_js(self, eid: Optional[str]) -> bool:
        """用 DOM click() 触发。分页按钮的可点宽度可能只有 8px，坐标点不可靠。"""
        if not eid:
            return False
        try:
            return bool(self.js("W.clickEl(" + json.dumps(eid) + ")"))
        except cdp.CDPError:
            return False

    def first_page(self, max_clicks: int = 300) -> bool:
        """回到第一页，**并验证真的到了第 1 页**。

        不能只看 `current == '1'` 或 `prevDisabled` 就相信：页面在后台放一段时间后
        Vue 组件会恢复上次页码，读到的状态可能是过期的。必须实际跳一次并复核。
        """
        pg = self.pager()
        if not pg.get("found"):
            return False
        if self.goto_page(1, verify_timeout=10):
            return True

        # 兜底：逐页翻（每 12 次停顿，打散节奏）
        for i in range(max_clicks):
            pg = self.pager()
            if not pg.get("found"):
                return False
            if pg.get("prevDisabled") or not pg.get("prevId"):
                return (pg.get("current") or "").strip() == "1"
            if not self.click_js(pg.get("prevId")):
                return False
            self.sleep(self.page_wait * random.uniform(0.9, 1.4))
            if i and i % 12 == 0:
                self.sleep(random.uniform(3.0, 6.0))
        return False

    def first_sig(self) -> str:
        try:
            return str(self.jcall("firstItemSig") or "")
        except cdp.CDPError:
            return ""

    def next_page(self, wait_change: bool = True, timeout: float = 8.0) -> bool:
        """翻到下一页。

        wait_change=True 时会等列表首条日期真的变化才返回。视频号翻页后数据
        是异步到达的，固定 sleep 1.6 秒经常扫到旧数据，极端情况下
        nextDisabled 还会提前为真，导致「138 页只扫到 17 页」。
        """
        pg = self.pager()
        if not pg.get("found") or pg.get("nextDisabled") or not pg.get("nextId"):
            return False
        before = self.first_sig() if wait_change else ""
        if not self.click_js(pg.get("nextId")):
            return False
        if not wait_change:
            self.sleep(self.page_wait)
            return True
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.sleep(0.4)
            now = self.first_sig()
            if now and now != before:
                self.sleep(0.35)  # 让整页渲染稳一下
                return True
        self.r.debug("翻页后列表未变化（超时 %.1fs），当前签名 %r", timeout, before)
        return False

    def prev_page(self, wait_change: bool = True, timeout: float = 8.0) -> bool:
        pg = self.pager()
        if not pg.get("found") or pg.get("prevDisabled") or not pg.get("prevId"):
            return False
        before = self.first_sig() if wait_change else ""
        if not self.click_js(pg.get("prevId")):
            return False
        if not wait_change:
            self.sleep(self.page_wait)
            return True
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.sleep(0.4)
            now = self.first_sig()
            if now and now != before:
                self.sleep(0.35)
                return True
        return False

    def pager_detail(self) -> dict:
        try:
            return self.jcall("pagerDetail") or {"found": False}
        except cdp.CDPError:
            return {"found": False}

    def goto_page(self, n: int, verify_timeout: float = 8.0) -> bool:
        """跳到第 n 页。

        两条路：
          1) 页码按钮可见（分页器一般只显示 1 2 3 4 ... N）→ 直接点
          2) 中间页码点不到 → 用跳转输入框：聚焦 → insertText → 点「跳转」
        两条都失败再退回「上一页/下一页」逐页翻。
        """
        n = int(n)
        if n < 1:
            return False

        # 1) 点可见页码
        try:
            res = self.jcall("clickPageNum", n)
        except cdp.CDPError:
            res = None
        if res and res.get("ok"):
            self.sleep(self.page_wait)
            if self._verify_page(n, verify_timeout):
                self.r.debug("跳页成功（页码按钮）-> %d", n)
                return True

        # 2) 跳转输入框
        if self._goto_by_input(n, verify_timeout):
            return True

        # 3) 逐页翻
        return self._goto_by_steps(n, verify_timeout)

    def _goto_by_input(self, n: int, verify_timeout: float) -> bool:
        try:
            prep = self.jcall("prepareJumpInput")
        except cdp.CDPError:
            return False
        if not prep or not prep.get("ok"):
            return False
        eid = prep.get("id")
        if not eid:
            return False

        # 真实点击聚焦（DOM focus() 有时不生效）
        try:
            self.click_id(eid, settle=0.1)
        except cdp.CDPError:
            pass
        self.sleep(0.2)

        # 全选后插入，避免和已有内容拼接成 "1137"
        try:
            self.s.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2,
            })
            self.s.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2,
            })
            self.sleep(0.1)
            self.s.send("Input.insertText", {"text": str(n)})
        except cdp.CDPError:
            return False
        self.sleep(0.2)

        try:
            sub = self.jcall("submitJump", prep.get("jumpId"))
        except cdp.CDPError:
            sub = None
        if sub and sub.get("ok"):
            self.sleep(self.page_wait)
            if self._verify_page(n, verify_timeout):
                self.r.debug("跳页成功（输入框/%s）-> %d", sub.get("how"), n)
                return True
        return False

    def _goto_by_steps(self, n: int, verify_timeout: float) -> bool:
        for _ in range(min(n, 400)):
            pg = self.pager()
            cur = (pg.get("current") or "").strip()
            if cur == str(n):
                return True
            if cur and cur.isdigit():
                step = 1 if int(cur) < n else -1
            else:
                step = 1
            ok = self.click_js(pg.get("prevId") if step < 0 else pg.get("nextId"))
            if not ok:
                return False
            self.sleep(self.page_wait * random.uniform(0.9, 1.4))
        return self._verify_page(n, verify_timeout)

    def _verify_page(self, n: int, timeout: float) -> bool:
        end = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < end:
            pg = self.pager()
            cur = (pg.get("current") or "").strip()
            if cur == str(n):
                return True
            self.sleep(0.35)
        return False

    def scroll_top(self) -> None:
        try:
            self.js(
                "var r=W.scrollRoot();var t=r||document.scrollingElement||document.documentElement;"
                "t.scrollTop=0;return t.scrollTop;"
            )
        except cdp.CDPError:
            pass

    # ---------------- 点击 ----------------
    def center(self, eid: str, do_scroll: bool = True) -> Optional[dict]:
        try:
            return self.jcall("center", eid, do_scroll)
        except cdp.CDPError:
            return None

    def click_id(self, eid: str, settle: float = 0.06) -> bool:
        """点一个元素。先滚进视口，等坐标稳定后再派发真实鼠标事件。"""
        c = self.center(eid, True)
        if not c:
            return False
        self.sleep(0.14)  # 等 scrollIntoView 落定（页面可能配了 smooth）
        c2 = self.center(eid, False) or c
        if not c2.get("inView"):
            return False
        x, y = c2["x"], c2["y"]
        if not (0 <= x <= 100000 and 0 <= y <= 100000):
            return False
        try:
            self.mouse.click(x, y, settle=settle)
        except cdp.CDPError as e:
            self.r.warn("点击派发失败: %s", e)
            return False
        return True

    def click_point(self, x: float, y: float) -> bool:
        try:
            self.mouse.click(x, y)
            return True
        except cdp.CDPError as e:
            self.r.warn("坐标点击派发失败: %s", e)
            return False

    def hover_point(self, x: float, y: float) -> bool:
        try:
            self.mouse.move(x, y)
            return True
        except cdp.CDPError:
            return False

    # ---------------- 元素查找 ----------------
    def find_texts(self, names: List[str], limit: int = 20) -> List[dict]:
        try:
            return self.jcall("findTexts", names, limit) or []
        except cdp.CDPError:
            return []

    def find_dialog(self) -> Optional[dict]:
        try:
            return self.jcall("findDialog", self._confirm_texts + ["取消", "提示"]) or None
        except cdp.CDPError:
            return None

    def dialog_button(self, dialog: dict) -> Optional[dict]:
        """在弹窗矩形内找确认按钮，避免误命中列表里同名的「删除」。"""
        if not dialog:
            return None
        dr = dialog.get("rect") or {}
        for name in self._confirm_texts:
            hits = [h for h in self.find_texts([name], 30) if _inside(h["rect"], dr)]
            if hits:
                # 优先 button 标签，其次面积最大的（主按钮通常更宽）
                hits.sort(key=lambda h: (h.get("tag") != "button", -h["rect"]["w"] * h["rect"]["h"]))
                return hits[0]
        return None

    # ---------------- 截图 ----------------
    def screenshot(self) -> Optional[bytes]:
        try:
            return self.s.screenshot()
        except cdp.CDPError as e:
            self.r.debug("CDP 截图失败: %s", e)
            return None

    # ---------------- 导航 ----------------
    def goto(self, url: str, wait: float = 2.5) -> None:
        self.s.navigate(url, wait=wait)
        self.s.set_active()

    def probe(self) -> dict:
        try:
            return self.jcall("probe") or {}
        except cdp.CDPError as e:
            return {"error": str(e)}


def _inside(inner: dict, outer: dict) -> bool:
    if not inner or not outer:
        return False
    pad = 2
    return (
        inner["left"] >= outer["left"] - pad
        and inner["top"] >= outer["top"] - pad
        and inner["right"] <= outer["right"] + pad
        and inner["bottom"] <= outer["bottom"] + pad
    )
