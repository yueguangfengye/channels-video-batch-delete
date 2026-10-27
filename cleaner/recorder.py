"""日志与失败截图记录。

两个通道：
1. 人类可读日志  logs/run-<runid>.log   （控制台同步输出）
2. 结构化操作日志 logs/ops.jsonl         （每步一行 JSON，便于事后统计/复盘）
3. 失败截图      shots/<runid>/<seq>_<tag>.png + shots/<runid>/index.jsonl

需求 6「运行完毕删除所有截图」由 purge_shots() 实现：删除前会把截图的
文件名与对应的失败摘要写进日志，信息不丢，只是不再留图片文件。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _stdio_utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


class Recorder:
    """一次运行一个实例，线程安全。"""

    def __init__(self, log_dir: Path, shot_dir: Path, run_id: str, verbose: bool = True):
        _stdio_utf8()
        self.run_id = run_id
        self.log_dir = Path(log_dir)
        self.shot_dir = Path(shot_dir) / run_id
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._seq = 0
        self.shots: list[dict] = []

        self.logger = logging.getLogger(f"cc.{run_id}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.handlers.clear()

        logfile = self.log_dir / f"run-{run_id}.log"
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
        self.logger.addHandler(fh)

        if verbose:
            sh = logging.StreamHandler(sys.stdout)
            sh.setLevel(logging.INFO)
            sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
            self.logger.addHandler(sh)

        self.logfile = logfile
        self._ops_path = self.log_dir / "ops.jsonl"

    # ---------------- 基础日志 ----------------
    def info(self, msg: str, *a: Any) -> None:
        self.logger.info(msg, *a)

    def warn(self, msg: str, *a: Any) -> None:
        self.logger.warning(msg, *a)

    def error(self, msg: str, *a: Any) -> None:
        self.logger.error(msg, *a)

    def debug(self, msg: str, *a: Any) -> None:
        self.logger.debug(msg, *a)

    # ---------------- 结构化操作日志 ----------------
    def op(self, action: str, **kv: Any) -> None:
        """记录一步操作。action 例：scan / click_delete / confirm / verify / pause。"""
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "action": action, "run": self.run_id}
        rec.update(kv)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with self._ops_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        self.logger.info("[%s] %s", action, " ".join(f"{k}={v}" for k, v in kv.items() if v not in (None, "")))

    # ---------------- 截图 ----------------
    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def save_shot(self, png_bytes: Optional[bytes], tag: str, note: str = "") -> Optional[Path]:
        """保存一张截图并登记到 index，返回路径。png_bytes 为 None 时跳过。"""
        if not png_bytes:
            return None
        seq = self.next_seq()
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in tag)[:48]
        path = self.shot_dir / f"{seq:04d}_{safe}.png"
        try:
            path.write_bytes(png_bytes)
        except Exception as e:  # 磁盘/权限问题不应中断流程
            self.warn("截图写入失败: %s (%s)", path, e)
            return None
        rec = {"seq": seq, "file": str(path.relative_to(self.shot_dir)), "tag": tag, "note": note, "ts": time.time()}
        self.shots.append(rec)
        with self._lock:
            try:
                with (self.shot_dir / "index.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
        self.debug("截图已保存 %s", path.name)
        return path

    # ---------------- 收尾 ----------------
    def purge_shots(self) -> int:
        """删除本次运行产生的全部截图，返回删除数量。

        删除前把清单写进日志，保证「哪天失败过、什么原因」仍可追溯。
        """
        removed = 0
        index = self.shot_dir / "index.jsonl"
        if self.shots or index.exists():
            self.info("---- 清理截图：本次共 %d 张 ----", len(self.shots))
            for rec in self.shots:
                self.info("  shot %04d  %-28s %s", rec["seq"], rec["tag"], rec.get("note", ""))
        if index.exists():
            try:
                index.unlink()
            except Exception:
                pass
        if self.shot_dir.exists():
            for p in sorted(self.shot_dir.rglob("*"), reverse=True):
                if p.is_file():
                    try:
                        p.unlink()
                        if p.suffix.lower() == ".png":
                            removed += 1
                    except Exception:
                        pass
                elif p.is_dir() and p is not self.shot_dir:
                    try:
                        p.rmdir()
                    except Exception:
                        pass
            try:
                self.shot_dir.rmdir()
            except Exception:
                pass
        self.info("已删除截图 %d 张", removed)
        return removed
