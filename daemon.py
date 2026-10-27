#!/usr/bin/env python3
"""常驻守护：反复执行删除，直到 state 里没有待处理项。

设计要点
    * 每轮重新连接 Edge —— 页面刷新、标签重开、CDP 断线都能自愈
    * 每轮都重载 config.yaml —— 改参数不用重启守护
    * 内容管理限制由 Cleaner 内部处理（记录 → 立即探测 → 继续，最多 5 次）
    * 连续 3 轮一条都没删掉就停手，避免空转刷请求
    * 响应 .stop 文件 / Ctrl+C，优雅退出并保存断点
    * 每次循环把进度写到 logs/daemon-status.json，方便外部查看

用法
    python daemon.py                # 跑到全部完成
    python daemon.py --max-rounds 5 # 最多跑 5 轮
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from cleaner.config import Config
from cleaner.dates import in_range, parse_bound, parse_publish_time
from cleaner.flow import Cleaner, LoginRequired
from cleaner.page import EdgePage
from cleaner.recorder import Recorder
from cleaner.state import DONE_STATUSES, State
from cleaner.throttle import StopRequested, install_sigint


def count_pending(state: State, start, end) -> int:
    n = 0
    for k, rec in state.targets.items():
        if state.status_of(k) in DONE_STATUSES:
            continue
        pt = parse_publish_time(rec.get("time", ""))
        if in_range(pt.value, start, end):
            n += 1
    return n


def write_status(path: Path, **kv) -> None:
    try:
        old = {}
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                old = {}
        old.update(kv)
        old["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        path.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="守护式批量删除，直到全部完成")
    ap.add_argument("-c", "--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--max-rounds", type=int, default=0, help="最多跑多少轮（0=不限）")
    ap.add_argument("--round-gap", type=float, default=20.0, help="两轮之间的间隔秒数")
    ap.add_argument("--max-stall", type=int, default=3, help="连续几轮零进展就停手")
    args = ap.parse_args()

    stop = threading.Event()
    install_sigint(stop)
    status_path = ROOT / "logs" / "daemon-status.json"
    stall = 0
    round_no = 0
    total_deleted = 0

    print("=" * 70)
    print("  守护式批量删除已启动（Ctrl+C 或 .stop 文件可中止）")
    print("=" * 70, flush=True)

    while not stop.is_set():
        if (ROOT / ".stop").exists():
            print("[stop] 检测到 .stop 文件，退出")
            break
        if args.max_rounds and round_no >= args.max_rounds:
            print(f"[done] 已达 --max-rounds {args.max_rounds}")
            break

        round_no += 1
        cfg = Config.load(Path(args.config))
        start = parse_bound(cfg.get("range.start", "2022-01-01"))
        end = parse_bound(cfg.get("range.end", "2025-12-31"))
        state = State(cfg.state_path, f"{start}..{end}")
        state.rollover_day()

        pending = count_pending(state, start, end)
        print(f"\n{'=' * 70}\n[第 {round_no} 轮] 剩余待删 {pending} 条 | 累计已删 {total_deleted}", flush=True)
        write_status(status_path, round=round_no, pending=pending,
                     total_deleted=total_deleted, state="running")
        if pending <= 0:
            print("[完成] 没有待处理项了，退出")
            write_status(status_path, state="finished", pending=0)
            break

        run_id = f"d{datetime.now().strftime('%m%d-%H%M%S')}r{round_no}"
        rec = Recorder(cfg.log_dir, cfg.shot_dir, run_id, verbose=True)
        session = None
        deleted_now = 0
        try:
            from run import open_session  # 复用 run.py 里的连接逻辑

            session = open_session(cfg, rec, allow_launch=True)
            page = EdgePage(session, cfg, rec)
            cleaner = Cleaner(cfg, rec, page, state, stop)
            stats = cleaner.execute(mode="run", limit=0, skip_collect=True)
            deleted_now = stats.deleted
            total_deleted += deleted_now
            print(f"[本轮] 删除 {deleted_now} 条；{stats.summary()}", flush=True)
            write_status(status_path, total_deleted=total_deleted, state="running")
        except StopRequested:
            print("\n[stop] 收到中止信号，保存断点后退出")
            try:
                state.save()
            except Exception:
                pass
            break
        except LoginRequired as e:
            rec.error("登录失效：%s", e)
            print(f"\n[需要人工] {e}\n  在 Edge 窗口重新扫码后，重新运行本守护脚本即可。")
            write_status(status_path, state="login_required")
            try:
                state.save()
            except Exception:
                pass
            return 2
        except SystemExit as e:
            rec.error("连接失败：%s", e)
            print(f"[连接失败] {e}")
        except KeyboardInterrupt:
            break
        except BaseException as e:  # noqa: BLE001
            rec.error("本轮异常：%s", e)
            traceback.print_exc()
        finally:
            try:
                state.save()
            except Exception:
                pass
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

        # 进展判断
        if deleted_now <= 0:
            stall += 1
            print(f"[无进展] 连续 {stall}/{args.max_stall} 轮没删掉任何视频", flush=True)
            if stall >= args.max_stall:
                print("[停止] 连续多轮无进展，可能存在未处理的限制，请查看 logs/ 后再决定")
                write_status(status_path, state="stalled")
                break
        else:
            stall = 0

        if not stop.is_set():
            gap = args.round_gap
            print(f"[等待] {gap:.0f} 秒后进入下一轮 ...", flush=True)
            stop.wait(gap)

    write_status(status_path, state="exited", total_deleted=total_deleted)
    print("\n" + "=" * 70)
    print(f"  守护进程结束：共 {round_no} 轮，累计删除 {total_deleted} 条")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
