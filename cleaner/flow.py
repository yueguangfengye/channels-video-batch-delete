"""业务流程编排。

阶段：
    guard      检查登录 / 验证码 / 风控
    collect    滚动遍历列表，收集条目
    delete     逐个删除（列表页 → 进垃圾桶）
    trash      切到垃圾桶 → 彻底删除

所有阶段共享：随机间隔、单日上限、熔断、暂停标志、断点状态。
"""
from __future__ import annotations

import csv
import hashlib
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from .dates import in_range, parse_bound, parse_publish_time
from .locator import Locator, Outcome
from .page import EdgePage, Item
from .restriction import RestrictionGuard
from .state import (
    DONE_STATUSES,
    STATUS_DELETED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_TRASHED,
    State,
)
from .throttle import Breaker, Cooldown, Pauser, StopRequested, Throttle, sleep_interruptible


class LoginRequired(RuntimeError):
    """登录态失效，需要人工扫码。"""


class NeedHuman(RuntimeError):
    """验证码 / 风控，需要人工处理后恢复。"""


@dataclass
class Stats:
    scanned: int = 0
    in_range: int = 0
    deleted: int = 0
    trashed: int = 0
    failed: int = 0
    skipped: int = 0
    started: float = field(default_factory=time.time)

    def summary(self) -> str:
        dur = int(time.time() - self.started)
        return (
            f"扫描 {self.scanned} 条 / 命中范围 {self.in_range} 条 | "
            f"列表删除 {self.deleted} | 垃圾桶彻底删除 {self.trashed} | "
            f"失败 {self.failed} | 跳过 {self.skipped} | 用时 {dur // 60}m{dur % 60}s"
        )


class Cleaner:
    def __init__(
        self,
        cfg,
        rec,
        page: EdgePage,
        state: State,
        stop: threading.Event,
    ):
        self.cfg = cfg
        self.rec = rec
        self.page = page
        self.state = state
        self.stop = stop
        self.loc = Locator(page, cfg, rec)
        self.throttle = Throttle(cfg, rec)
        self.breaker = Breaker(cfg, rec)
        self.pauser = Pauser(cfg, rec)
        self.cooldown = Cooldown(cfg, rec)
        self.restriction = RestrictionGuard(cfg, rec, page, state)
        self.max_retry = max(0, int(cfg.get("limits.max_retry_per_item", 2)))
        self.stats = Stats()

        self.start: date = parse_bound(cfg.get("range.start", "2022-01-01"))
        self.end: date = parse_bound(cfg.get("range.end", "2025-12-31"))
        self.daily_max = int(cfg.get("limits.daily_max", 60) or 0)
        self.scroll_pause = float(cfg.get("limits.scroll_pause", 1.2))
        self.page_wait = float(cfg.get("limits.page_wait", 1.6))
        self.max_pages = int(cfg.get("limits.max_pages", 400))
        self.max_scroll_rounds = int(cfg.get("limits.max_scroll_rounds", 400))
        self.idle_limit = int(cfg.get("limits.max_idle_scrolls", 5))
        self.rescan_rounds = max(1, int(cfg.get("limits.rescan_rounds", 3)))
        self.row_timeout = float(cfg.get("verify.row_disappear_timeout", 8.0))
        self.shot_on_fail = bool(cfg.get("verify.shot_on_failure", True))

        self._login_signals = list(cfg.get("selectors.login_signals", []))
        self._captcha_signals = list(cfg.get("selectors.captcha_signals", []))
        self._risk_signals = list(cfg.get("selectors.risk_signals", []))
        self._all_signals = self._login_signals + self._captcha_signals + self._risk_signals

    # ==================== 前置检查 ====================
    def guard(self) -> None:
        q = self.page.quick(self._all_signals)
        url = q.get("url") or ""
        low = url.lower()
        hit = set(q.get("hits") or [])

        if "login" in low or (hit & set(self._login_signals)):
            self.rec.op("signal", kind="login_required", url=url[:120], matched=sorted(hit & set(self._login_signals)))
            raise LoginRequired("Edge 里视频号登录态已失效，请在该 Edge 窗口重新扫码登录")

        for kw in self._captcha_signals:
            if kw in hit:
                self.rec.op("signal", kind="captcha", keyword=kw)
                self.pauser.pause_and_wait(self.stop, f"检测到验证码：{kw}")
                return

        for kw in self._risk_signals:
            if kw in hit:
                self.rec.op("signal", kind="risk", keyword=kw)
                # 内容管理限制：完整记录现场 -> 按配置等待(默认 0 秒) -> 探测恢复 -> 继续
                self.restriction.handle(
                    signal=kw,
                    rule=f"selectors.risk_signals ← 命中 {kw!r}",
                    stop=self.stop,
                    kind="content_management_restriction",
                )
                return

    # ==================== 条目构造 ====================
    def make_item(self, raw: dict, phase: str) -> Item:
        title = (raw.get("title") or "").strip()
        time_text = (raw.get("timeText") or "").strip()
        pt = parse_publish_time(time_text)
        key = hashlib.sha1(f"{title}|{time_text}".encode("utf-8")).hexdigest()[:16]
        return Item(
            key=key,
            title=title,
            time_text=time_text,
            date_str=pt.value.isoformat() if pt.value else "",
            in_range=in_range(pt.value, self.start, self.end),
            card_id=raw.get("cardId") or "",
            rect=raw.get("rect") or {},
            action_id=raw.get("actionId"),
            action_text=raw.get("actionText"),
            phase=phase,
        )

    # ==================== 收集 ====================
    def collect(self, phase: str = "list") -> Dict[str, Item]:
        """收集列表全部条目。

        视频号后台是「分页」而不是无限滚动。先正常扫一遍；如果一条都没扫到，
        几乎一定是接口被限流（页面会渲染成「你还没有发表过视频」），此时按
        按配置冷却（默认 0 秒，立即）后重扫，而不是直接判定「没有视频」。
        """
        found = self._scan_all(phase)

        # 和历史记录比对：一次运行内不可能少掉大半，少了一定是数据没加载全
        expected = len(self.state.targets)
        if found and expected >= 200 and len(found) < expected * 0.6:
            self.rec.warn("只扫到 %d 条，历史记录 %d 条，疑似数据加载不全", len(found), expected)
            self.restriction.handle(
                signal=f"列表数据不完整：{len(found)} 条（历史 {expected} 条）",
                rule=f"collect 完整性校验 ← 阈值 60%（{len(found)} < {expected}×0.6）",
                stop=self.stop,
                kind="data_truncated",
            )
            again = self._scan_all(phase)
            if len(again) > len(found):
                self.rec.op("scan_recovered", first=len(found), second=len(again))
                found = again

        if found:
            return found

        self.rec.op("scan_empty", phase=phase, note="0 条，疑似接口限流，进入重试")
        self.cooldown.trigger("列表 0 条，疑似接口限流", self.stop)
        found = self._scan_all(phase)
        if not found:
            raise RuntimeError(
                "扫描到 0 条视频：接口被限流或超时（页面显示「你还没有发表过视频」）。"
                "这不是数据丢失，请等几小时后再跑。断点已保存。"
            )
        return found

    def _scan_all(self, phase: str) -> Dict[str, Item]:
        found: Dict[str, Item] = {}
        if not self.page.first_page():
            self.rec.op("scan_note", phase=phase, msg="没有找到分页器，退化为滚动模式")

        page_no = 0
        idle = 0
        while page_no < self.max_pages:
            page_no += 1
            self.pauser.check_stop(self.stop)
            self.pauser.wait_if_paused(self.stop, f"扫描{phase}")
            self.restriction.set_context(phase=phase, page_no=page_no)
            self.guard()

            raws = self.page.scan_list()
            new = 0
            for raw in raws:
                it = self.make_item(raw, phase)
                if it.key not in found:
                    found[it.key] = it
                    new += 1
            self.rec.op("scan_page", phase=phase, page=page_no, visible=len(raws), new=new, total=len(found))

            pg = self.page.pager()
            if not pg.get("found"):
                # 没有分页器 -> 退回滚动加载
                info = self.page.scroll(0.85)
                idle = 0 if new else idle + 1
                if info.get("atBottom") or idle >= self.idle_limit:
                    break
                self.page.sleep(self.scroll_pause)
                continue

            # 用 total 判断边界，不信 nextDisabled（连点下一页时它会被提前置真，
            # 实测扫到第 10 页就报"最后一页"，其实有 138 页）
            total = int(pg.get("total") or 0)
            if total and page_no >= total:
                self.rec.op("scan_end", phase=phase, reason=f"已到最后一页(共{total}页)", total=len(found))
                break

            # 逐页"跳转"而不是连点「下一页」：后者翻十几次就会被平台限流，
            # 总页数被压缩成 10 页、数据只剩 192 条；跳页则稳定 2.4 秒一页。
            if not self.page.goto_page(page_no + 1):
                self.rec.op("scan_end", phase=phase, reason=f"跳第 {page_no + 1} 页失败", total=len(found))
                break
            self.page.sleep(random.uniform(0.4, 1.0))
        return found

    # ==================== 单条删除 ====================
    def delete_one(self, item: Item, phase: str, attempt: int = 1) -> bool:
        label = "列表删除" if phase == "list" else "彻底删除"
        self.rec.op("delete_begin", phase=phase, label=label, title=item.short(30),
                    time=item.time_text, attempt=attempt)

        out: Outcome = self.loc.open_delete(item)
        if not out.ok:
            return self._fail_or_retry(item, phase, "no_dialog", out.detail, attempt)

        out2 = self.loc.confirm()
        if not out2.ok:
            self._dismiss_dialog()
            return self._fail_or_retry(item, phase, "no_confirm", out2.detail, attempt)

        if not self.loc.wait_removed(item, self.row_timeout):
            return self._fail_or_retry(item, phase, "still_present", "确认后条目仍在列表中", attempt)

        status = STATUS_DELETED if phase == "list" else STATUS_TRASHED
        self.state.mark(item.key, status, phase=phase)
        if phase == "list":
            self.state.bump("deleted_today")
            self.state.bump("total_deleted")
            self.stats.deleted += 1
        else:
            self.state.bump("trash_today")
            self.state.bump("total_trash")
            self.stats.trashed += 1
        self.state.save()
        self.breaker.ok()
        self.cooldown.reset()
        self.rec.op("delete_ok", phase=phase, title=item.short(30), how=out.how, confirm=out2.how)
        self.throttle.after_action(self.stop)
        return True

    def _fail_or_retry(self, item: Item, phase: str, tag: str, detail: str, attempt: int) -> bool:
        """失败后按配置冷却（默认 0 秒，立即重试），超过次数上限才记为失败。"""
        if attempt <= self.max_retry:
            self.rec.op("retry_scheduled", phase=phase, title=item.short(24), tag=tag,
                        attempt=attempt, of=self.max_retry, detail=detail[:120])
            try:
                self.cooldown.trigger(f"{tag}: {detail}", self.stop)
            except StopRequested:
                self._on_fail(item, phase, tag, detail + "（等待期间被中止）")
                raise
            return self.delete_one(item, phase, attempt + 1)
        self._on_fail(item, phase, tag, detail)
        return False

    def _on_fail(self, item: Item, phase: str, tag: str, detail: str) -> None:
        self.stats.failed += 1
        self.state.mark(item.key, STATUS_FAILED, detail, phase=phase)
        self.state.bump("failures")
        self.state.save()
        self.rec.op("delete_fail", phase=phase, title=item.short(30), tag=tag, detail=detail)
        if self.shot_on_fail:
            png = self.page.screenshot()
            self.rec.save_shot(png, f"{phase}_{item.key[:8]}_{tag}", note=f"{item.title[:50]} | {detail}")
        self.breaker.fail(f"{tag}: {detail}")
        # 失败后也要喘一口，否则连续快速点击会直接触发风控
        sleep_interruptible(random.uniform(1.2, 2.8), self.stop)

    def _dismiss_dialog(self) -> None:
        """失败时把弹窗关掉，避免遮挡后续操作。"""
        try:
            hits = self.page.find_texts(list(self.cfg.get("selectors.cancel_texts", ["取消"])), 10)
            if hits:
                self.page.click_id(hits[0]["id"])
                self.page.sleep(0.3)
        except Exception:
            pass

    # ==================== 删除循环 ====================
    def _sweep(self, remaining: set, phase: str, backward: bool) -> None:
        """按页码逐页跳转并处理。

        每页内部「扫描 → 删一条 → 重扫」反复执行：删掉一条后服务器总数变化，
        后面的条目会前移补进当前页，不重扫就会漏。
        """
        pg = self.page.pager()
        total = int(pg.get("total") or 0) or self.max_pages
        cur_raw = (pg.get("current") or "").strip()
        cur = int(cur_raw) if cur_raw.isdigit() else 1
        pages = list(range(cur, 0, -1)) if backward else list(range(cur, total + 1))
        self.rec.op("sweep_plan", phase=phase, start=cur, total=total,
                    direction="backward" if backward else "forward", pages=len(pages))

        for idx, pno in enumerate(pages):
            if not remaining:
                break
            if idx > 0:
                if not self.page.goto_page(pno):
                    self.rec.op("page_jump_fail", phase=phase, page=pno)
                    break
                self.page.sleep(random.uniform(0.5, 1.4))

            self.pauser.check_stop(self.stop)
            self.pauser.wait_if_paused(self.stop, phase)
            self.restriction.set_context(phase=phase, page_no=pno)
            self.guard()
            self._check_caps(phase)

            guard_loop = 0
            while remaining and guard_loop < 400:
                guard_loop += 1
                # 每条之间都检查停止信号：一页 20 条删完要一两分钟，
                # 只在换页时检查会导致 .stop 长时间不生效。
                self.pauser.check_stop(self.stop)
                live: Dict[str, Item] = {}
                for raw in self.page.scan_list():
                    it = self.make_item(raw, phase)
                    live[it.key] = it
                todo_items = [live[k] for k in remaining if k in live]
                if not todo_items:
                    break
                # 从列表**最下面**一条开始删。视频号删除后不会自动刷新列表，
                # 若从上往下删，上面少一条会让后面所有条目整体上移，下一次
                # 扫描到的"第 2 条"其实已经是"第 3 条"——直接删错视频。
                # 倒着删则上方索引完全不受影响。
                item = max(todo_items, key=lambda it: it.rect.get("top", 0))
                self.restriction.set_context(current_item=item.title)
                self.delete_one(item, phase)
                remaining.discard(item.key)

            self.rec.op("page_done", phase=phase, page=pno, left=len(remaining))

    def run_delete_loop(self, keys: List[str], phase: str) -> None:
        """逐页删除。

        方向选择很关键：collect 刚跑完时通常停在**最后一页**，此时直接倒序往回走。
        如果先调 first_page() 连点 137 次「上一页」，会在几分钟内打出上百次翻页
        请求，平台会直接返回「操作过于频繁」——这是实测踩到的坑。

        倒序还有个额外好处：删除只会让条目向小页码方向前移，倒序时那些页还没处理，
        所以不会漏；正序则可能漏掉前移到已处理页的条目。
        """
        remaining = set(keys)
        pg = self.page.pager()
        total = int(pg.get("total") or 0)
        cur_raw = (pg.get("current") or "").strip()
        at_last = bool(total and cur_raw.isdigit() and int(cur_raw) >= total)

        if at_last:
            self.rec.op("sweep_begin", phase=phase, direction="backward", left=len(remaining))
            self._sweep(remaining, phase, backward=True)
        else:
            self.rec.op("sweep_begin", phase=phase, direction="forward", left=len(remaining))
            self._sweep(remaining, phase, backward=False)

        for r in range(2, self.rescan_rounds + 1):
            if not remaining:
                break
            self.rec.op("rescan_round", phase=phase, round=r, left=len(remaining))
            self.page.first_page()
            self._sweep(remaining, phase, backward=False)

        if remaining:
            for k in remaining:
                rec = self.state.targets.get(k, {})
                self.rec.op("skip", key=k, reason="多轮遍历后仍未找到", title=(rec.get("title") or "")[:30])
                self.state.mark(k, STATUS_SKIPPED, "多轮遍历后仍未在列表中找到")
                self.stats.skipped += 1
            self.state.save()
        self.rec.op("loop_end", phase=phase, left=len(remaining))

    def _check_caps(self, phase: str) -> None:
        if self.daily_max <= 0:
            return
        used = self.state.deleted_today()
        if used >= self.daily_max:
            self.state.save()
            raise NeedHuman(
                f"已达到单日上限 {self.daily_max} 条（今日已处理 {used} 条）。"
                f"如需继续，调大 config.yaml 的 limits.daily_max，或明天再跑。断点已保存。"
            )

    # ==================== 垃圾桶 ====================
    def switch_trash(self) -> bool:
        names = list(self.cfg.get("urls.trash_tab_text", ["垃圾桶", "回收站"]))
        hits = self.page.find_texts(names, 10)
        if hits:
            self.rec.op("switch_trash", how="tab", text=hits[0]["text"])
            self.page.click_id(hits[0]["id"])
            self.page.sleep(1.5)
            return True
        url = (self.cfg.get("urls.trash_url") or "").strip()
        if url:
            self.rec.op("switch_trash", how="url", url=url)
            self.page.goto(url, wait=2.5)
            return True
        self.rec.warn("没找到垃圾桶入口：页面没有 %s 文本，也没配 trash_url", names)
        return False

    def run_trash_phase(self) -> None:
        if not self.cfg.get("trash.enabled", True):
            self.rec.op("trash_skip", reason="配置已关闭")
            return
        if not self.switch_trash():
            self.rec.op("trash_skip", reason="无法进入垃圾桶")
            return
        info = self.page.info()
        self.rec.op("trash_page", url=(info.get("url") or "")[:120], head=(info.get("bodyHead") or "")[:200])

        items = self.collect("trash")
        targets = [it for it in items.values() if it.in_range]
        todo = [it.key for it in targets if self.state.status_of(it.key) != STATUS_TRASHED]
        self.rec.op("trash_plan", total=len(items), in_range=len(targets), todo=len(todo))
        if not todo:
            return
        self.run_delete_loop(todo, "trash")

    # ==================== 清单输出 ====================
    def report(self, items: Dict[str, Item]) -> str:
        rows = sorted(items.values(), key=lambda i: (i.date_str or "0000-00-00", i.title))
        out = self.cfg.log_dir / f"dryrun-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["序号", "发布日期", "是否在范围", "标题", "时间原文", "key"])
            for i, it in enumerate(rows, 1):
                w.writerow([i, it.date_str or "无法解析", "是" if it.in_range else "否", it.title, it.time_text, it.key])

        hit = [r for r in rows if r.in_range]
        self.rec.info("=" * 78)
        self.rec.info("待删除清单（范围内 %d 条 / 共扫描 %d 条）", len(hit), len(rows))
        self.rec.info("%-4s %-12s %-46s %s", "序号", "发布日期", "标题", "时间原文")
        for i, it in enumerate(hit[:80], 1):
            self.rec.info("%-4d %-12s %-46s %s", i, it.date_str or "?", it.short(44), it.time_text)
        if len(hit) > 80:
            self.rec.info("…… 其余 %d 条见 CSV", len(hit) - 80)
        self.rec.info("=" * 78)
        self.rec.info("清单已写入: %s", out)
        return str(out)

    # ==================== 主流程 ====================
    def execute(self, mode: str = "run", limit: int = 0, skip_collect: bool = False) -> Stats:
        self.rec.op("begin", mode=mode, start=str(self.start), end=str(self.end), daily_max=self.daily_max)
        self.restriction.set_context(phase="预检", page_no=0)
        self.guard()

        # 跳过全量收集：直接复用 state 里已有的待处理键（省 5 分钟扫描）
        if skip_collect and self.state.targets:
            todo = []
            for k, rec0 in self.state.targets.items():
                if self.state.status_of(k) in DONE_STATUSES:
                    continue
                # state 里没有 in_range 标记，用记录的时间文本重新判定，
                # 否则会把范围外的（2026 年）也排进来
                pt = parse_publish_time(rec0.get("time", ""))
                if not in_range(pt.value, self.start, self.end):
                    continue
                todo.append(k)
            self.rec.op("skip_collect", targets=len(todo))
            if limit > 0:
                todo = todo[:limit]
                self.rec.op("limit_applied", limit=limit)
            if todo:
                self.run_delete_loop(todo, "list")
            self.run_trash_phase()
            return self.stats

        items = self.collect("list")
        self.stats.scanned = len(items)
        ranged = {k: v for k, v in items.items() if v.in_range}
        self.stats.in_range = len(ranged)
        self.rec.op("scan_summary", scanned=len(items), in_range=len(ranged))

        if mode == "dry-run":
            self.report(items)
            return self.stats

        for k, it in items.items():
            self.state.upsert(k, it.title, it.time_text, "list")
        self.state.save()

        todo = [k for k in ranged if self.state.status_of(k) not in DONE_STATUSES]
        if limit > 0:
            todo = todo[:limit]
            self.rec.op("limit_applied", limit=limit)
        self.rec.info("本轮待删 %d 条（范围内共 %d 条）", len(todo), len(ranged))

        if todo:
            self.run_delete_loop(todo, "list")
        self.run_trash_phase()
        return self.stats
