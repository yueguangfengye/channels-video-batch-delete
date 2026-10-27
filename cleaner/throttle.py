"""节流、熔断与暂停控制。

三个东西：
    Throttle : 随机间隔 + 周期性长休
    Breaker  : 连续失败熔断，防止页面异常时继续瞎点
    Pauser   : 标志文件驱动的暂停 / 恢复 / 中断

为什么暂停用标志文件而不是控制台输入：
程序在后台跑，如果你去点控制台窗口敲回车，就等于把前台交出去了。
所以约定：项目根目录出现 `.pause` → 下一次检查即暂停；
出现 `.resume`（或配置了 auto_resume_after 超时）→ 继续。全程不碰键鼠。
想彻底停下来，写一个 `.stop` 文件，或在本机另开终端 Ctrl+C。
"""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path


class StopRequested(Exception):
    """收到中断信号（Ctrl+C / .stop 文件 / stop event）。"""


class Throttle:
    def __init__(self, cfg, recorder):
        self.cfg = cfg
        self.r = recorder
        self.count = 0
        self._mtime = 0.0
        self._reload()
        # 记住 config.yaml 的修改时间，之后每次休眠前比对，变了就重读
        try:
            p = getattr(cfg, "path", None)
            self._mtime = p.stat().st_mtime if p else 0.0
        except Exception:
            self._mtime = 0.0

    def _reload(self) -> None:
        self._lo = float(self.cfg.get("limits.min_delay", 1.25))
        self._hi = max(self._lo, float(self.cfg.get("limits.max_delay", 3.75)))
        self._every = int(self.cfg.get("limits.long_pause_every", 12) or 0)
        self._lmin = float(self.cfg.get("limits.long_pause_min", 20))
        self._lmax = max(self._lmin, float(self.cfg.get("limits.long_pause_max", 45)))

    def _maybe_reload(self) -> None:
        """config.yaml 改了就地重载，不必重启进程。

        重载失败（比如写文件写到一半）时沿用旧值，绝不让节流参数变成非法值。
        """
        p = getattr(self.cfg, "path", None)
        if not p:
            return
        try:
            m = p.stat().st_mtime
        except Exception:
            return
        if m == self._mtime:
            return
        self._mtime = m
        try:
            from .config import Config

            old = (self._lo, self._hi)
            self.cfg = Config.load(p)
            self._reload()
            self.r.op(
                "throttle_reload",
                old=f"{old[0]}~{old[1]}",
                new=f"{self._lo}~{self._hi}",
                long_every=self._every,
                long_pause=f"{self._lmin}~{self._lmax}",
            )
        except Exception as e:
            self.r.warn("节流配置重载失败，沿用旧值：%s", e)

    def wait(self, stop: threading.Event) -> None:
        """两条视频之间的随机间隔。"""
        self._maybe_reload()
        delay = random.uniform(self._lo, self._hi)
        self.r.op("throttle", seconds=round(delay, 2))
        sleep_interruptible(delay, stop)

    def after_action(self, stop: threading.Event) -> None:
        self.count += 1
        self.wait(stop)
        self._maybe_reload()
        if self._every and self.count % self._every == 0:
            d = random.uniform(self._lmin, self._lmax)
            self.r.op("long_pause", seconds=round(d, 1), done=self.count)
            sleep_interruptible(d, stop)


class Breaker:
    """连续失败达到阈值即熔断。"""

    def __init__(self, cfg, recorder):
        self.r = recorder
        self.limit = int(cfg.get("limits.max_consecutive_failures", 4) or 0)
        self.fails = 0

    def ok(self) -> None:
        if self.fails:
            self.r.debug("连续失败计数归零（此前 %d 次）", self.fails)
        self.fails = 0

    def fail(self, reason: str = "") -> None:
        self.fails += 1
        self.r.op("failure_streak", consecutive=self.fails, reason=reason[:200])
        if self.limit and self.fails >= self.limit:
            raise RuntimeError(f"连续失败 {self.fails} 次，已熔断停止本轮。最后原因：{reason[:200]}")


class Cooldown:
    """请求受限 / 操作失败后的强制冷却，默认 20 分钟后自动重试。

    和 Pauser 的区别：
        Pauser   —— 遇到验证码这类必须人工介入的情况，阻塞等 `.resume`
        Cooldown —— 遇到频率限制这类会自愈的情况，睡够时间自动继续
    冷却期间仍然响应 `.stop` / Ctrl+C，可以随时中止。
    """

    def __init__(self, cfg, recorder):
        self.r = recorder
        self.seconds = float(cfg.get("limits.cooldown_seconds", 1200))
        self.max_rounds = int(cfg.get("limits.max_cooldown_rounds", 6) or 0)
        self.rounds = 0
        self._until = 0.0

    @property
    def active(self) -> bool:
        return time.monotonic() < self._until

    def trigger(self, reason: str, stop: threading.Event) -> None:
        self.rounds += 1
        if self.max_rounds and self.rounds > self.max_rounds:
            raise RuntimeError(
                f"本轮已连续冷却 {self.rounds - 1} 次仍未恢复（最后原因：{reason[:120]}）。"
                f"平台限制可能较重，建议隔几小时再跑。断点已保存。"
            )
        self._until = time.monotonic() + self.seconds
        self.r.op(
            "cooldown_begin",
            round=self.rounds,
            seconds=int(self.seconds),
            until=time.strftime("%H:%M:%S", time.localtime(time.time() + self.seconds)),
            reason=reason[:160],
        )
        end = self._until
        while True:
            if stop.is_set():
                raise StopRequested()
            left = end - time.monotonic()
            if left <= 0:
                break
            stop.wait(min(2.0, left))
        self.r.op("cooldown_end", round=self.rounds, reason=reason[:160])

    def reset(self) -> None:
        """成功执行后归零，避免偶发失败累积到上限。"""
        if self.rounds:
            self.r.debug("冷却计数归零（此前 %d 轮）", self.rounds)
        self.rounds = 0


class Pauser:
    """标志文件驱动的暂停器，不抢占前台。"""

    def __init__(self, cfg, recorder):
        self.r = recorder
        self.pause_flag: Path = cfg.abspath(cfg.get("pause.pause_flag", ".pause"))
        self.resume_flag: Path = cfg.abspath(cfg.get("pause.resume_flag", ".resume"))
        self.stop_flag: Path = cfg.abspath(".stop")
        self.auto = float(cfg.get("pause.auto_resume_after", 0) or 0)
        self.beep = bool(cfg.get("pause.beep", True))

    # ---------------- 中断 ----------------
    def check_stop(self, stop: threading.Event) -> None:
        if stop.is_set():
            raise StopRequested()
        if self.stop_flag.exists():
            try:
                self.stop_flag.unlink()
            except Exception:
                pass
            raise StopRequested()

    # ---------------- 被动暂停 ----------------
    def wait_if_paused(self, stop: threading.Event, reason: str = "手动") -> None:
        if not self.pause_flag.exists():
            return
        self.r.op("pause", reason=reason, resume_by="写 .resume 文件或等 auto_resume_after")
        if self.beep:
            _beep()
        started = time.monotonic()
        while True:
            self.check_stop(stop)
            if self.resume_flag.exists():
                try:
                    self.resume_flag.unlink()
                except Exception:
                    pass
                break
            if self.auto and (time.monotonic() - started) >= self.auto:
                self.r.op("auto_resume", waited=round(time.monotonic() - started, 1))
                break
            stop.wait(0.5)
        try:
            if self.pause_flag.exists():
                self.pause_flag.unlink()
        except Exception:
            pass
        self.r.op("resume", reason=reason)

    # ---------------- 主动暂停 ----------------
    def pause_now(self, reason: str) -> None:
        """程序主动进入暂停（遇到验证码 / 登录失效 / 风控提示时）。"""
        self.r.op("pause", reason=reason, auto="程序主动暂停，处理后写 .resume")
        try:
            self.pause_flag.write_text(reason, encoding="utf-8")
        except Exception:
            pass
        if self.beep:
            _beep()

    def pause_and_wait(self, stop: threading.Event, reason: str) -> None:
        """主动暂停并阻塞，直到人工处理后写 .resume（验证码 / 风控场景）。"""
        self.pause_now(reason)
        started = time.monotonic()
        while True:
            self.check_stop(stop)
            if self.resume_flag.exists():
                try:
                    self.resume_flag.unlink()
                except Exception:
                    pass
                break
            if self.auto and (time.monotonic() - started) >= self.auto:
                self.r.op("auto_resume", waited=round(time.monotonic() - started, 1))
                break
            stop.wait(0.5)
        try:
            if self.pause_flag.exists():
                self.pause_flag.unlink()
        except Exception:
            pass
        self.r.op("resume", reason=reason)


def sleep_interruptible(seconds: float, stop: threading.Event, tick: float = 0.2) -> None:
    """可被立即中断的 sleep。"""
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        if stop.is_set():
            raise StopRequested()
        left = end - time.monotonic()
        if left <= 0:
            return
        stop.wait(min(tick, left))


def _beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(0x00000040)  # MB_ICONASTERISK
    except Exception:
        pass


def install_sigint(stop: threading.Event) -> None:
    """把 Ctrl+C 转成优雅停止，保证 state 落盘。"""

    def _handler(signum, frame):  # noqa: ARG001
        stop.set()

    try:
        import signal

        signal.signal(signal.SIGINT, _handler)
        try:
            signal.signal(signal.SIGBREAK, _handler)
        except Exception:
            pass
    except Exception:
        pass
