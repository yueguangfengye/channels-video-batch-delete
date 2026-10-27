"""内容管理限制的识别、记录与自动恢复。

职责：
    1. 限制发生时，把「具体内容 / 触发的规则 / 时间点 / 现场上下文」完整落盘
    2. 等待配置的冷却时长（默认 1 小时）后自动继续
    3. 重试前做恢复探测，满足全部条件才继续执行
    4. 多次重试仍被限制时，生成报告、进入人工暂停，不再盲目重试

产出：
    logs/restrictions.jsonl       每次限制一条结构化记录（含探测结果）
    logs/RESTRICTION-REPORT.md    人类可读的汇总报告（每次限制后刷新）
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .throttle import StopRequested


@dataclass
class RestrictionEvent:
    """一次限制事件的完整现场。"""

    ts: str                      # 发生时间点（精确到秒）
    kind: str                    # content_management_restriction / data_truncated / delete_blocked
    signal: str                  # 命中的原文，例如「操作过于频繁」
    rule: str                    # 触发规则，例如 selectors.risk_signals ← 命中 '操作过于频繁'
    phase: str = ""              # list / trash
    page_no: int = 0             # 当时在第几页
    current_item: str = ""       # 当时正在处理的视频标题
    url: str = ""
    page_text: str = ""          # 页面正文片段（限制提示的原文）
    video_count: int = 0         # 页面顶部「视频 (N)」的 N
    pager_total: int = 0         # 分页器总页数
    items_visible: int = 0       # 当前页可见条目数
    deleted_today: int = 0       # 今日已删条数
    cooldown_seconds: int = 0    # 本次冷却时长
    retry_index: int = 1         # 第几次重试
    max_retries: int = 5
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeResult:
    ts: str
    retry_index: int
    recovered: bool
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)


class RestrictionLog:
    def __init__(self, log_dir: Path, report_name: str = "RESTRICTION-REPORT.md"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.log_dir / "restrictions.jsonl"
        self.report_path = self.log_dir / report_name

    # ---------------- 写入 ----------------
    def record(self, item: RestrictionEvent | ProbeResult) -> None:
        rec = asdict(item)
        rec["_type"] = "event" if isinstance(item, RestrictionEvent) else "probe"
        try:
            with self.jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        self.write_report()

    # ---------------- 读取 ----------------
    def entries(self) -> List[dict]:
        if not self.jsonl.exists():
            return []
        out = []
        try:
            for line in self.jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        except Exception:
            return []
        return out

    def summary(self) -> Dict[str, Any]:
        evs = [e for e in self.entries() if e.get("_type") == "event"]
        probes = [e for e in self.entries() if e.get("_type") == "probe"]
        return {
            "events": len(evs),
            "probes": len(probes),
            "recovered": sum(1 for p in probes if p.get("recovered")),
            "last": evs[-1] if evs else None,
        }

    # ---------------- 报告 ----------------
    def write_report(self) -> Optional[Path]:
        evs = [e for e in self.entries() if e.get("_type") == "event"]
        probes = [e for e in self.entries() if e.get("_type") == "probe"]
        if not evs:
            return None

        lines = [
            "# 内容管理限制记录报告",
            "",
            f"> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"> 累计限制 {len(evs)} 次，恢复探测 {len(probes)} 次，"
            f"其中成功恢复 {sum(1 for p in probes if p.get('recovered'))} 次",
            "",
            "## 一、限制明细",
            "",
            "| # | 时间点 | 类型 | 命中原文 | 触发规则 | 位置 | 当时页面视频数 |",
            "|---|---|---|---|---|---|---|",
        ]
        for i, e in enumerate(evs, 1):
            lines.append(
                f"| {i} | {e.get('ts','')} | {e.get('kind','')} | "
                f"{(e.get('signal') or '-')[:28]} | {(e.get('rule') or '-')[:46]} | "
                f"{e.get('phase','')} 第{e.get('page_no',0)}页 | {e.get('video_count',0)} |"
            )

        lines += ["", "## 二、每次限制的现场上下文", ""]
        for i, e in enumerate(evs, 1):
            lines += [
                f"### {i}. {e.get('ts','')} · {e.get('kind','')}",
                "",
                f"- **命中原文**：`{e.get('signal','-')}`",
                f"- **触发规则**：`{e.get('rule','-')}`",
                f"- **发生位置**：{e.get('phase','')} 第 {e.get('page_no',0)} 页"
                f"，当时处理：{e.get('current_item') or '-'}",
                f"- **页面地址**：{e.get('url','-')}",
                f"- **页面视频总数**：{e.get('video_count',0)}"
                f"　**分页总页数**：{e.get('pager_total',0)}"
                f"　**当前页可见条目**：{e.get('items_visible',0)}",
                f"- **今日已删**：{e.get('deleted_today',0)} 条",
                f"- **本次冷却**：{e.get('cooldown_seconds',0)} 秒"
                f"　**重试序号**：{e.get('retry_index',1)} / {e.get('max_retries',5)}",
            ]
            if e.get("page_text"):
                lines += ["", "```text", str(e["page_text"])[:600], "```"]
            lines.append("")

        if probes:
            lines += ["## 三、恢复探测记录", "", "| # | 时间 | 第几次重试 | 是否恢复 | 判定依据 |", "|---|---|---|---|---|"]
            for i, p in enumerate(probes, 1):
                lines.append(
                    f"| {i} | {p.get('ts','')} | {p.get('retry_index',0)} | "
                    f"{'✅ 已恢复' if p.get('recovered') else '❌ 仍受限'} | {(p.get('reason') or '')[:60]} |"
                )
            lines.append("")

        try:
            self.report_path.write_text("\n".join(lines), encoding="utf-8")
            return self.report_path
        except Exception:
            return None


class RestrictionGuard:
    """限制处理：记录 → 等待 → 探测 → 重试 or 升级。"""

    def __init__(self, cfg, recorder, page, state):
        self.cfg = cfg
        self.r = recorder
        self.page = page
        self.state = state
        self.log = RestrictionLog(cfg.log_dir, cfg.get("restriction.report_file", "RESTRICTION-REPORT.md"))
        self.cooldown = int(cfg.get("restriction.cooldown_seconds", 3600) or 3600)
        self.max_retries = int(cfg.get("restriction.max_retries", 5) or 5)
        self.min_page_ratio = float(cfg.get("restriction.min_page_ratio", 0.9))
        # 上下文（由 flow 在运行中更新）
        self.phase = "list"
        self.page_no = 0
        self.current_item = ""

    # ---------------- 上下文 ----------------
    def set_context(self, phase: str = "", page_no: int = 0, current_item: str = "") -> None:
        if phase:
            self.phase = phase
        if page_no:
            self.page_no = page_no
        if current_item:
            self.current_item = current_item

    # ---------------- 现场快照 ----------------
    def _snapshot(self) -> Dict[str, Any]:
        import re

        info: Dict[str, Any] = {"url": "", "text": "", "video_count": 0,
                                "pager_total": 0, "items_visible": 0}
        try:
            q = self.page.quick([])
            info["url"] = (q.get("url") or "")[:300]
            txt = self.page.js("W.visibleText(1200)") or ""
            info["text"] = txt[:1200]
            m = re.search(r"视频\s*[（(](\d+)[)）]", txt)
            if m:
                info["video_count"] = int(m.group(1))
            pg = self.page.pager()
            info["pager_total"] = int(pg.get("total") or 0)
            info["items_visible"] = len(self.page.scan_list())
        except Exception:
            pass
        return info

    # ---------------- 主入口 ----------------
    def handle(self, signal: str, rule: str, stop, kind: str = "content_management_restriction") -> None:
        """遇到限制时调用。正常恢复则返回，反复失败则抛 NeedHuman。"""
        for attempt in range(1, self.max_retries + 1):
            snap = self._snapshot()
            ev = RestrictionEvent(
                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                kind=kind,
                signal=signal,
                rule=rule,
                phase=self.phase,
                page_no=self.page_no,
                current_item=self.current_item[:80],
                url=snap["url"],
                page_text=snap["text"],
                video_count=snap["video_count"],
                pager_total=snap["pager_total"],
                items_visible=snap["items_visible"],
                deleted_today=self.state.deleted_today(),
                cooldown_seconds=self.cooldown,
                retry_index=attempt,
                max_retries=self.max_retries,
            )
            self.log.record(ev)
            self.r.op(
                "restriction",
                attempt=attempt,
                signal=signal[:80],
                rule=rule[:100],
                page=self.page_no,
                video_count=snap["video_count"],
                cooldown=self.cooldown,
                report=str(self.log.report_path),
            )

            # 等待（可被 .stop / Ctrl+C 打断）
            self._wait(self.cooldown, stop, attempt)

            ok, why, detail = self._probe()
            self.log.record(
                ProbeResult(
                    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    retry_index=attempt,
                    recovered=ok,
                    reason=why,
                    detail=detail,
                )
            )
            if ok:
                self.r.op("restriction_recovered", attempt=attempt, reason=why)
                return
            self.r.warn("第 %d/%d 次重试仍受限：%s", attempt, self.max_retries, why)

        # 全部重试失败 -> 升级为人工处理
        self._escalate(signal, rule)
        span = f"{self.cooldown // 60} 分钟" if self.cooldown >= 60 else f"{self.cooldown} 秒"
        raise RuntimeError(
            f"连续 {self.max_retries} 次等待 {span} 后仍被限制（{signal}）。"
            f"已停止自动重试，详见 {self.log.report_path}"
        )

    # ---------------- 等待 ----------------
    def _wait(self, seconds: int, stop, attempt: int) -> None:
        until = time.time() + seconds
        self.r.op(
            "restriction_wait",
            attempt=attempt,
            seconds=seconds,
            resume_at=time.strftime("%H:%M:%S", time.localtime(until)),
        )
        end = time.monotonic() + seconds
        last_log = 0.0
        while True:
            if stop.is_set():
                raise StopRequested()
            left = end - time.monotonic()
            if left <= 0:
                return
            # 每 10 分钟报一次进度，方便盯后台
            if time.monotonic() - last_log >= 600:
                last_log = time.monotonic()
                self.r.info("限制冷却中，剩余 %d 分 %d 秒", int(left) // 60, int(left) % 60)
            stop.wait(min(5.0, left))

    # ---------------- 恢复探测 ----------------
    def _probe(self) -> Tuple[bool, str, Dict[str, Any]]:
        """重试前必须全部满足：无限制提示 / 列表有数据 / 页数未缩水。"""
        detail: Dict[str, Any] = {}
        signals = list(self.cfg.get("selectors.risk_signals", [])) + \
                  list(self.cfg.get("selectors.captcha_signals", []))

        hit = self.page.hits(signals)
        detail["signals_hit"] = hit
        if hit:
            return False, f"页面仍有提示：{'、'.join(hit)}", detail

        try:
            raws = self.page.scan_list()
        except Exception as e:
            return False, f"扫描异常：{e}", detail
        detail["items_visible"] = len(raws)
        if not raws:
            return False, "列表 0 条（数据未加载）", detail

        pg = self.page.pager()
        total = int(pg.get("total") or 0)
        detail["pager_total"] = total
        if total <= 0:
            return False, "分页器缺失", detail

        expected = int(self.cfg.get("restriction.expected_pages", 0) or 0)
        if expected and total < expected * self.min_page_ratio:
            return False, f"总页数缩水：{total} < 预期 {expected}", detail

        detail["first_item"] = raws[0].get("timeText", "")
        return True, f"恢复正常（{len(raws)} 条，共 {total} 页）", detail

    # ---------------- 升级 ----------------
    def _escalate(self, signal: str, rule: str) -> None:
        path = self.log.write_report()
        self.r.op("restriction_escalate", signal=signal[:80], rule=rule[:100], report=str(path))
        try:
            flag = Path(self.cfg.abspath(self.cfg.get("pause.pause_flag", ".pause")))
            span = f"{self.cooldown // 60} 分钟" if self.cooldown >= 60 else f"{self.cooldown} 秒"
            flag.write_text(
                f"内容管理限制：{signal}\n"
                f"已连续 {self.max_retries} 次等待 {span} 后仍未恢复。\n"
                f"详情见 {path}\n"
                f"人工处理后删除本文件（或写 .resume）即可继续。",
                encoding="utf-8",
            )
        except Exception:
            pass
