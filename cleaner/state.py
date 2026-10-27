"""断点续跑状态机。

state.json 结构：
{
  "version": 1,
  "range": "2022-01-01..2025-12-31",
  "stats": {"day": "2026-10-27", "deleted_today": 3, "trash_today": 2,
            "total_deleted": 12, "total_trash": 10, "failures": 1},
  "targets": {
    "<key>": {"title": "...", "time": "2022-03-05", "status": "pending",
              "phase": "list", "attempts": 0, "error": "", "updated": "..."}
  }
}

status 取值：
  pending  -> 待处理
  deleted  -> 列表页已删除（进了垃圾桶）
  trashed  -> 垃圾桶中已彻底删除
  failed   -> 多次重试失败，需人工介入
  skipped  -> 明确跳过（例如已不存在）
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

STATUS_PENDING = "pending"
STATUS_DELETED = "deleted"
STATUS_TRASHED = "trashed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

DONE_STATUSES = (STATUS_DELETED, STATUS_TRASHED, STATUS_SKIPPED)


class State:
    def __init__(self, path: Path, range_repr: str = ""):
        self.path = Path(path)
        self.range_repr = range_repr
        self.data = self._load()

    # ---------------- 持久化 ----------------
    def _blank(self) -> dict:
        return {
            "version": 1,
            "range": self.range_repr,
            "stats": {
                "day": date.today().isoformat(),
                "deleted_today": 0,
                "trash_today": 0,
                "total_deleted": 0,
                "total_trash": 0,
                "failures": 0,
            },
            "targets": {},
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._blank()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state 根节点不是对象")
            base = self._blank()
            base.update({k: v for k, v in data.items() if k in base})
            base["stats"].update(data.get("stats") or {})
            base["targets"] = data.get("targets") or {}
            return base
        except Exception:
            # 状态文件损坏时不阻断流程，直接新开一份（旧文件会被备份）
            try:
                self.path.replace(self.path.with_suffix(".corrupt.json"))
            except Exception:
                pass
            return self._blank()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---------------- 统计 ----------------
    @property
    def stats(self) -> dict:
        return self.data["stats"]

    def rollover_day(self) -> bool:
        """跨天则重置当日计数，返回是否发生了重置。"""
        today = date.today().isoformat()
        if self.stats.get("day") != today:
            self.stats["day"] = today
            self.stats["deleted_today"] = 0
            self.stats["trash_today"] = 0
            return True
        return False

    def deleted_today(self) -> int:
        return int(self.stats.get("deleted_today", 0)) + int(self.stats.get("trash_today", 0))

    def bump(self, field: str, n: int = 1) -> None:
        self.stats[field] = int(self.stats.get(field, 0)) + n

    # ---------------- 目标表 ----------------
    @property
    def targets(self) -> Dict[str, dict]:
        return self.data["targets"]

    def upsert(self, key: str, title: str, time_text: str, phase: str) -> dict:
        rec = self.targets.get(key)
        if rec is None:
            rec = {
                "title": title,
                "time": time_text,
                "status": STATUS_PENDING,
                "phase": phase,
                "attempts": 0,
                "error": "",
                "updated": "",
            }
            self.targets[key] = rec
        else:
            rec["title"] = title or rec.get("title", "")
            rec["time"] = time_text or rec.get("time", "")
        return rec

    def mark(self, key: str, status: str, error: str = "", phase: Optional[str] = None) -> None:
        rec = self.targets.get(key)
        if rec is None:
            return
        rec["status"] = status
        rec["error"] = (error or "")[:500]
        rec["updated"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        if status == STATUS_FAILED:
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
        if phase:
            rec["phase"] = phase

    def status_of(self, key: str) -> str:
        rec = self.targets.get(key)
        return rec.get("status", "") if rec else ""

    def pending(self, phase: Optional[str] = None) -> List[str]:
        out = []
        for k, rec in self.targets.items():
            if rec.get("status") in DONE_STATUSES:
                continue
            if phase and rec.get("phase") != phase:
                continue
            out.append(k)
        return out

    def count_by_status(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for rec in self.targets.values():
            s = rec.get("status", "?")
            out[s] = out.get(s, 0) + 1
        return out
