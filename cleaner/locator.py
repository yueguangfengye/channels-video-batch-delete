"""按钮定位与点击校验（多坐标点阶梯 + 模板匹配 + 结果校验）。

定位策略按可靠性从高到低排列，前一级成功就不进入下一级：

  ① DOM 元素点击 —— 找到按钮节点，取其包围盒中心。位置永远正确，
     从根本上解决「垃圾桶里删除按钮随标题长度漂移」的问题。
  ② 卡片内文本命中 —— 在卡片包围盒内按文本"删除 / 更多"再找一次。
  ③ hover 后重扫 —— 有些按钮要鼠标停在卡片上才显形。
  ④ 多组备用坐标点 —— 以卡片矩形为锚点算相对坐标，逐个尝试。
     每个点都会校验「确认弹窗是否出现」，没出现就换下一个点，
     所以不存在"点歪了还以为成功"。

每一级点击后都调用 _settle() 做校验：
    - 出现确认弹窗 → 成功
    - 出现浮层菜单 → 点菜单里的"删除" → 再校验
    - 都没有 → 失败，进入下一级
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .page import EdgePage, Item, _inside


@dataclass
class Outcome:
    ok: bool
    how: str = ""
    tries: int = 0
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


class Locator:
    def __init__(self, page: EdgePage, cfg, rec):
        self.page = page
        self.cfg = cfg
        self.r = rec
        self.delete_texts: List[str] = list(cfg.get("selectors.delete_texts", ["删除"]))
        self.more_texts: List[str] = list(cfg.get("selectors.more_texts", ["更多", "…", "..."]))
        self.confirm_texts: List[str] = list(cfg.get("selectors.confirm_texts", ["确定", "确认删除", "删除"]))
        self.delete_offsets: List[dict] = list(cfg.get("locate.delete_offsets", []))
        self.confirm_offsets: List[dict] = list(cfg.get("locate.confirm_offsets", []))
        self.wait = float(cfg.get("locate.click_verify_wait", 0.45))
        self.per_point = max(1, int(cfg.get("locate.attempts_per_point", 2)))
        self.dialog_timeout = float(cfg.get("verify.dialog_timeout", 3.5))
        self._dialog: Optional[dict] = None
        self._current_item: Optional[Item] = None

    # ---------------- 锚点 ----------------
    @staticmethod
    def anchor(rect: dict, name: str):
        table = {
            "card_tl": (rect.get("left", 0), rect.get("top", 0)),
            "card_tr": (rect.get("right", 0), rect.get("top", 0)),
            "card_bl": (rect.get("left", 0), rect.get("bottom", 0)),
            "card_br": (rect.get("right", 0), rect.get("bottom", 0)),
            "card_center": (rect.get("cx", 0), rect.get("cy", 0)),
            "dialog_bc": (rect.get("cx", 0), rect.get("bottom", 0)),
            "dialog_tc": (rect.get("cx", 0), rect.get("top", 0)),
            "dialog_center": (rect.get("cx", 0), rect.get("cy", 0)),
        }
        return table.get(name, (rect.get("cx", 0), rect.get("cy", 0)))

    # ---------------- 打开删除入口 ----------------
    def open_delete(self, item: Item) -> Outcome:
        """让确认弹窗出现。成功后 self._dialog 就是那个弹窗。"""
        self._dialog = None
        self._current_item = item
        tries = 0

        # ① 已知 action 元素（"删除"或"更多"按钮）
        if item.action_id:
            tries += 1
            self.r.op("click_target", how="dom-action", text=item.action_text, title=item.short(18))
            if self.page.click_id(item.action_id) and self._settle():
                return Outcome(True, "dom-action", tries, f"按钮文本={item.action_text}")

        # ② 卡片包围盒内按文本找
        hit = self._find_in_card(item, self.delete_texts + self.more_texts)
        if hit:
            tries += 1
            self.r.op("click_target", how="dom-text-in-card", text=hit["text"], title=item.short(18))
            if self.page.click_id(hit["id"]) and self._settle():
                return Outcome(True, "dom-text", tries, f"卡片内文本={hit['text']}")

        # ③ hover 让按钮显形后重扫
        for i in range(2):
            tries += 1
            self._hover_card(item)
            fresh = self._rescan_item(item)
            if fresh and fresh.action_id:
                self.r.op("click_target", how="hover-rescan", attempt=i + 1, title=item.short(18))
                if self.page.click_id(fresh.action_id) and self._settle():
                    return Outcome(True, "hover-rescan", tries, "悬停后显形")
            hit = self._find_in_card(fresh or item, self.delete_texts)
            if hit:
                if self.page.click_id(hit["id"]) and self._settle():
                    return Outcome(True, "hover-text", tries, "")

        # ④ 多组备用坐标点
        # 走到这里说明前面的 click_id 可能已经滚动过页面，item.rect 是上一屏的
        # 视口坐标，直接拿它算偏移会点歪。必须先按 card_id 重新取一次最新矩形。
        self.page.press_escape()
        self.page.sleep(0.2)
        base_rect = item.rect
        c = self.page.center(item.card_id, True)
        if c and c.get("rect"):
            base_rect = c["rect"]
            self.page.sleep(0.15)

        for off in self.delete_offsets:
            ax, ay = self.anchor(base_rect, off.get("anchor", "card_tr"))
            x, y = ax + float(off.get("dx", 0)), ay + float(off.get("dy", 0))
            for attempt in range(self.per_point):
                tries += 1
                self.r.op(
                    "click_target",
                    how="coordinate",
                    point=off.get("name"),
                    xy=(round(x), round(y)),
                    attempt=attempt + 1,
                )
                if not self.page.click_point(x, y):
                    break
                if self._settle():
                    return Outcome(True, "coordinate", tries, f"坐标点={off.get('name')} ({round(x)},{round(y)})")
                self.page.sleep(0.25)

        return Outcome(False, "none", tries, "所有定位方式均未唤出确认弹窗")

    def _settle(self) -> bool:
        """点击后的校验：弹窗出现？还是冒出浮层菜单？"""
        self.page.sleep(self.wait)
        dlg = self._wait_dialog(min(self.dialog_timeout, 2.0))
        if dlg:
            return True
        # 可能点的是「更多」，需要再点浮层菜单里的「删除」
        menu = self._find_menu_item(self._current_item)
        if menu:
            self.r.op("click_target", how="menu-item", text=menu["text"])
            if self.page.click_id(menu["id"]):
                if self._wait_dialog(self.dialog_timeout):
                    return True
        # 失败：把可能弹出的浮层关掉，否则下一次点击会落在菜单上
        self.page.press_escape()
        self.page.sleep(0.15)
        return False

    def _find_in_card(self, item: Item, texts: List[str]) -> Optional[dict]:
        hits = [h for h in self.page.find_texts(texts, 30) if _inside(h["rect"], item.rect)]
        if not hits:
            return None
        hits.sort(key=lambda h: h["rect"]["w"] * h["rect"]["h"])  # 取最内层
        return hits[0]

    def _find_menu_item(self, item: Optional[Item] = None) -> Optional[dict]:
        """找浮层菜单里的「删除」。

        必须排除卡片自身的删除按钮——它的可点宽度只有几十像素，
        会被误当成菜单项，导致在同一条视频上反复点、反复弹窗。
        """
        hits = self.page.find_texts(self.delete_texts, 30)
        out = []
        for h in hits:
            if item is not None and _inside(h["rect"], item.rect):
                continue
            out.append(h)
        if not out:
            return None
        out.sort(key=lambda h: h["rect"]["w"] * h["rect"]["h"])
        return out[0]

    def _hover_card(self, item: Item) -> None:
        r = item.rect
        try:
            self.page.hover_point(r["left"] + min(160, r["w"] * 0.35), r["cy"])
            self.page.sleep(0.35)
        except Exception:
            pass

    def _rescan_item(self, item: Item) -> Optional[Item]:
        try:
            raws = self.page.scan_list()
        except Exception:
            return None
        best = None
        best_d = 1e9
        for raw in raws:
            d = abs(raw["rect"]["cy"] - item.rect["cy"])
            if d < best_d:
                best_d, best = d, raw
        if best is None or best_d > 160:
            return None
        return Item(
            key=item.key,
            title=best.get("title") or item.title,
            time_text=best.get("timeText") or item.time_text,
            date_str=item.date_str,
            in_range=item.in_range,
            card_id=best["cardId"],
            rect=best["rect"],
            action_id=best.get("actionId"),
            action_text=best.get("actionText"),
            phase=item.phase,
        )

    def _wait_dialog(self, timeout: float) -> Optional[dict]:
        end = time.monotonic() + max(0.3, timeout)
        while time.monotonic() < end:
            d = self.page.find_dialog()
            if d:
                self._dialog = d
                return d
            time.sleep(0.12)
        return None

    # ---------------- 确认弹窗 ----------------
    def confirm(self) -> Outcome:
        dlg = self._dialog or self.page.find_dialog()
        if not dlg:
            return Outcome(False, "no-dialog", 0, "没有检测到确认弹窗")
        # 尺寸兜底：拿到不可信的小矩形时宁可失败，也不要按它算坐标去乱点
        dr = dlg.get("rect") or {}
        if dr.get("w", 0) < 200 or dr.get("h", 0) < 70:
            return Outcome(False, "bad-dialog", 0, f"弹窗尺寸异常，放弃点击 {dr}")

        btn = self.page.dialog_button(dlg)
        if btn:
            self.r.op("confirm_click", how="dom", text=btn["text"])
            if self.page.click_id(btn["id"]):
                if self._wait_gone(dlg):
                    return Outcome(True, "dom", 1, f"按钮文本={btn['text']}")

        tries = 1
        for off in self.confirm_offsets:
            ax, ay = self.anchor(dlg["rect"], off.get("anchor", "dialog_bc"))
            x, y = ax + float(off.get("dx", 0)), ay + float(off.get("dy", 0))
            tries += 1
            self.r.op("confirm_click", how="coordinate", point=off.get("name"), xy=(round(x), round(y)))
            if not self.page.click_point(x, y):
                continue
            if self._wait_gone(dlg):
                return Outcome(True, "coordinate", tries, f"坐标点={off.get('name')}")
        return Outcome(False, "none", tries, "确认按钮定位失败")

    def _wait_gone(self, dlg: dict, timeout: float = 1.5) -> bool:
        """弹窗消失即认为确认成功（后续还会校验条目是否真的消失）。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            cur = self.page.find_dialog()
            if not cur:
                return True
            time.sleep(0.15)
        return False

    # ---------------- 结果校验 ----------------
    def wait_removed(self, item: Item, timeout: float) -> bool:
        """判断删除是否成功。

        注意：视频号删除后**不会自动刷新列表**，条目仍留在 DOM 里，所以
        「条目消失」这个信号经常拿不到。真正的判据是顶部 toast：
        出现「已删除」= 成功；出现「操作过于频繁」等风控词 = 失败。
        两者都没有时，弹窗已关闭即可乐观认定成功（真正的漏删由多轮收敛兜底）。
        """
        success_words = list(self.cfg.get("selectors.success_signals", ["删除成功", "已删除"]))
        fail_words = list(self.cfg.get("selectors.risk_signals", [])) + ["删除失败", "操作失败"]
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = set(self.page.hits(success_words + fail_words))
            if got & set(fail_words):
                return False
            if got & set(success_words):
                return True
            try:
                raws = self.page.scan_list()
            except Exception:
                raws = []
            if not any(self._same(raw, item) for raw in raws):
                return True
            time.sleep(0.45)
        return True

    @staticmethod
    def _same(raw: dict, item: Item) -> bool:
        t1 = (raw.get("title") or "").strip()
        t2 = (item.title or "").strip()
        if t1 and t2 and t1[:20] != t2[:20]:
            return False
        return (raw.get("timeText") or "").strip() == (item.time_text or "").strip()
