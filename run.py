#!/usr/bin/env python3
"""视频号批量删除工具 —— 命令行入口。

    python run.py selftest        离线自检（不连网、不动浏览器）
    python run.py probe           连通性 / 登录态检查
    python run.py inspect         打印页面结构，用于校准选择器
    python run.py dry-run         只扫描、只出清单，不点任何按钮
    python run.py run --limit 3   小样本试跑（删 3 条）
    python run.py run --yes       正式全量运行
    python run.py purge-shots     清空 shots 目录
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _utf8() -> None:
    for name in ("stdout", "stderr"):
        st = getattr(sys, name, None)
        if st is not None and hasattr(st, "reconfigure"):
            try:
                st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_utf8()

from cleaner import cdp, edge, vision  # noqa: E402
from cleaner.config import Config  # noqa: E402
from cleaner.flow import Cleaner, LoginRequired, NeedHuman  # noqa: E402
from cleaner.page import EdgePage  # noqa: E402
from cleaner.recorder import Recorder  # noqa: E402
from cleaner.state import State  # noqa: E402
from cleaner.throttle import StopRequested, install_sigint  # noqa: E402

EXAMPLES = """
常用流程：
    1) 双击 start-edge.bat        # 启动带调试端口的 Edge（独立 profile），扫码登录视频号
    2) python run.py probe        # 确认能连上、登录态有效
    3) python run.py inspect      # 看一眼页面结构对不对
    4) python run.py dry-run      # 出待删清单，人工核对
    5) python run.py run --limit 3  # 小样本试删 3 条
    6) python run.py run --yes    # 正式全量
"""


# ---------------------------------------------------------------- 基础设施
def load_cfg(args) -> Config:
    path = Path(args.config)
    if not path.is_absolute():
        path = HERE / path
    cfg = Config.load(path)
    if not path.exists():
        print(f"[warn] 配置文件不存在，使用内置默认值: {path}")
    return cfg


def make_recorder(cfg: Config, verbose: bool = True) -> Recorder:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Recorder(cfg.log_dir, cfg.shot_dir, run_id, verbose=verbose)


def open_session(cfg: Config, rec: Recorder, allow_launch: bool = False) -> "cdp.Session":
    host = cfg.get("cdp.host", "127.0.0.1")
    port = int(cfg.get("cdp.port", 9222))

    if not edge.cdp_alive(host, port):
        ver = edge.cdp_version(host, port)
        if ver:
            rec.info("调试端口在线: %s", ver.get("Browser", "?"))
        else:
            if not allow_launch:
                raise SystemExit(
                    f"连不上 Edge 调试端口 {host}:{port}\n"
                    f"请先双击 start-edge.bat 启动带调试端口的 Edge（独立 profile，需要扫码登录一次），\n"
                    f"或加 --launch 让本程序自己启动。"
                )
            exe = cfg.get("edge.exe") or edge.find_edge()
            if not exe:
                raise SystemExit("没找到 msedge.exe，请在 config.yaml 的 edge.exe 里写绝对路径")
            profile = cfg.abspath(cfg.get("edge.profile_dir", ".edge-profile"))
            rec.op("launch_edge", exe=str(exe), port=port, profile=str(profile))
            edge.launch(Path(exe), port, profile, cfg.get("urls.post_list", ""), minimized=True)
            if not edge.wait_alive(host, port, 30):
                raise SystemExit("Edge 已启动但调试端口没起来（可能已有 Edge 实例在跑，参数被转交了）")

    targets = cdp.list_targets(host, port)
    if not targets:
        raise SystemExit("调试端口没有可用 target")

    match = cfg.get("cdp.match_url", "") or ""
    t = cdp.pick_target(targets, match)
    if t is None and match:
        rec.info("没有已打开的视频号页面，新建一个标签（不会抢焦点）")
        t = cdp.new_tab(host, port, cfg.get("urls.post_list", ""))
        if t:
            time.sleep(2.0)
    if t is None:
        rec.info("当前可操作目标：")
        for x in targets:
            rec.info("  %s", x)
        raise SystemExit("没找到可操作的页面，请先在 Edge 里打开视频号后台")

    rec.op("attach", target=f"[{t.type}] {t.title[:40]}")
    s = cdp.Session(t, timeout=float(cfg.get("cdp.timeout", 30)))
    s.set_active()
    return s


def purge_all_shots(shot_dir: Path) -> int:
    n = 0
    if not shot_dir.exists():
        return 0
    for p in sorted(shot_dir.rglob("*"), reverse=True):
        if p.is_file():
            try:
                p.unlink()
                if p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    n += 1
            except Exception:
                pass
        else:
            try:
                p.rmdir()
            except Exception:
                pass
    return n


def _finish(rec: Recorder, cfg: Config, session, keep_shots: bool) -> None:
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
    if not keep_shots and cfg.get("app.purge_shots_on_finish", True):
        rec.purge_shots()
        purge_all_shots(cfg.shot_dir)


# ---------------------------------------------------------------- 子命令
def cmd_selftest(args) -> int:
    cfg = load_cfg(args)
    results = []

    def chk(name, fn):
        try:
            detail = fn()
            results.append((True, name, str(detail or "")))
        except Exception as e:
            results.append((False, name, f"{type(e).__name__}: {e}"))

    # 1 配置
    def _cfg():
        s, e = cfg.get("range.start"), cfg.get("range.end")
        from cleaner.dates import parse_bound

        a, b = parse_bound(s), parse_bound(e)
        assert a <= b, "起始日期晚于结束日期"
        return f"{a} ~ {b}，daily_max={cfg.get('limits.daily_max')}"

    chk("配置加载", _cfg)

    # 2 日期解析
    def _dates():
        from cleaner.dates import parse_publish_time

        cases = {
            "2022-03-05": "2022-03-05",
            "2022/3/5 12:30": "2022-03-05",
            "2022年3月5日": "2022-03-05",
            "2022.03.05 08:00": "2022-03-05",
            "2025-12-31": "2025-12-31",
            "发布于 2023年11月9日 21:04": "2023-11-09",
        }
        bad = []
        for src, want in cases.items():
            got = parse_publish_time(src).value
            if got is None or got.isoformat() != want:
                bad.append(f"{src} -> {got} (期望 {want})")
        assert not bad, "; ".join(bad)
        return f"{len(cases)} 个用例全部通过"

    chk("发布时间解析", _dates)

    # 3 范围过滤
    def _range():
        from cleaner.dates import in_range, parse_bound

        a, b = parse_bound(cfg.get("range.start")), parse_bound(cfg.get("range.end"))
        assert in_range(parse_bound("2022-01-01"), a, b)
        assert in_range(parse_bound("2025-12-31"), a, b)
        assert not in_range(parse_bound("2021-12-31"), a, b)
        assert not in_range(parse_bound("2026-01-01"), a, b)
        return "边界判定正确（含首尾，排除范围外）"

    chk("时间范围过滤", _range)

    # 4 状态保存/加载
    def _state():
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td) / "s.json", "r")
            st.upsert("abc", "标题", "2022-01-01", "list")
            st.mark("abc", "deleted")
            st.bump("deleted_today")
            st.save()
            st2 = State(Path(td) / "s.json", "r")
            assert st2.status_of("abc") == "deleted"
            assert st2.stats["deleted_today"] == 1
            return "写入/回读一致"

    chk("断点状态机", _state)

    # 5 模板匹配
    def _vision():
        import numpy as np

        screen = np.zeros((200, 320, 3), dtype=np.uint8)
        screen[:] = (30, 30, 30)
        # 带纹理的模板：纯色块会让归一化相关系数退化，真实按钮也一样必须有纹理
        tpl = np.zeros((24, 60, 3), dtype=np.uint8)
        tpl[:] = (240, 240, 240)
        tpl[::4, :] = (20, 20, 20)
        tpl[:, ::7] = (60, 60, 60)
        screen[100:124, 150:210] = tpl
        m = vision.best(screen, tpl, threshold=0.9)
        assert m is not None, "没匹配到"
        assert abs(m.cx - 180) <= 3 and abs(m.cy - 112) <= 3, f"位置偏差 {m.cx},{m.cy}（期望 180,112）"
        assert not vision.is_blank(screen)
        assert vision.is_blank(np.zeros((40, 40, 3), dtype=np.uint8))
        return f"命中 {m}"

    chk("模板匹配", _vision)

    # 6 Win32 窗口枚举
    def _win32():
        wins = edge.enum_windows()
        assert len(wins) > 0, "枚举不到任何窗口"
        from cleaner import win32bg

        states = [win32bg.window_state(w["hwnd"]) for w in wins[:3]]
        return f"枚举 {len(wins)} 个窗口，前 3 个状态可用"

    chk("Win32 窗口枚举", _win32)

    # 7 页面脚本完整性
    def _dom():
        from cleaner import dom

        js = dom.JS_LIB
        assert len(js) > 2000, "JS 库过短"
        need = ["W.root", "W.qsa", "W.scanList", "W.findDialog", "W.pager",
                "W.hits", "W.clickEl", "W.titleOf", "W.actOf"]
        missing = [n for n in need if n not in js]
        assert not missing, f"缺少 {missing}"
        return f"{len(js)} 字符，{len(need)} 个关键函数齐全"

    chk("页面探针脚本", _dom)

    # 8 Edge 定位
    def _edge():
        exe = cfg.get("edge.exe") or edge.find_edge()
        assert exe, "没找到 msedge.exe"
        assert Path(exe).exists(), f"路径不存在: {exe}"
        return str(exe)

    chk("Edge 可执行文件", _edge)

    # 9 限制记录与自动恢复机制
    def _restriction():
        import tempfile

        from cleaner.restriction import ProbeResult, RestrictionEvent, RestrictionLog

        with tempfile.TemporaryDirectory() as td:
            log = RestrictionLog(Path(td))
            log.record(
                RestrictionEvent(
                    ts="2026-10-27 16:00:00",
                    kind="content_management_restriction",
                    signal="操作过于频繁",
                    rule="selectors.risk_signals ← 命中 '操作过于频繁'",
                    phase="list",
                    page_no=12,
                    video_count=2732,
                )
            )
            log.record(ProbeResult(ts="2026-10-27 17:00:00", retry_index=1, recovered=False, reason="仍受限"))
            s = log.summary()
            assert s["events"] == 1 and s["probes"] == 1, s
            assert log.report_path.exists(), "报告未生成"
            txt = log.report_path.read_text(encoding="utf-8")
            assert "操作过于频繁" in txt and "触发规则" in txt, "报告内容缺失"
            assert (Path(td) / "restrictions.jsonl").exists(), "jsonl 未生成"
        return (
            f"记录/探测/报告正常；冷却 {cfg.get('restriction.cooldown_seconds')}s × "
            f"{cfg.get('restriction.max_retries')} 次"
        )

    chk("限制记录机制", _restriction)

    # 10 调试端口（只报告，不算失败）
    host, port = cfg.get("cdp.host", "127.0.0.1"), int(cfg.get("cdp.port", 9222))
    ver = edge.cdp_version(host, port)
    port_ok = bool(ver)
    port_msg = f"{ver.get('Browser')}" if ver else f"{host}:{port} 未监听（运行前先跑 start-edge.bat）"

    # ---- 输出 ----
    print("=" * 74)
    print("自检结果")
    print("=" * 74)
    fails = 0
    for ok, name, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<16} {detail[:96]}")
        if not ok:
            fails += 1
    print(f"  [{'PASS' if port_ok else 'INFO'}] {'Edge 调试端口':<16} {port_msg}")
    print("-" * 74)
    print(f"  {len(results) - fails}/{len(results)} 通过" + ("" if port_ok else "（端口未开，属正常，走 start-edge.bat）"))
    print("=" * 74)
    return 0 if fails == 0 else 1


def cmd_probe(args) -> int:
    cfg = load_cfg(args)
    rec = make_recorder(cfg)
    session = None
    try:
        session = open_session(cfg, rec, allow_launch=args.launch)
        page = EdgePage(session, cfg, rec)
        info = page.info()
        rec.info("URL      : %s", info.get("url", ""))
        rec.info("标题     : %s", info.get("title", ""))
        rec.info("视口     : %s", info.get("viewport"))

        body = info.get("bodyHead") or ""
        low = (info.get("url") or "").lower()
        if "login" in low or "扫码登录" in body:
            rec.error("登录态：失效（需要扫码）")
            return 2
        rec.info("登录态：正常")

        for kw in cfg.get("selectors.captcha_signals", []):
            if kw and kw in body:
                rec.error("检测到验证码关键词：%s", kw)
                return 3
        for kw in cfg.get("selectors.risk_signals", []):
            if kw and kw in body:
                rec.warn("检测到风控关键词：%s", kw)

        items = page.scan_list()
        rec.info("当前视口可识别条目：%d 条", len(items))
        for it in items[:5]:
            rec.info("   · %-40s %s", (it.get("title") or "(无标题)")[:40], it.get("timeText"))
        if not items:
            rec.warn("没扫到条目：可能不在视频号动态列表页，或列表还没加载完。试试 python run.py inspect")

        wins = edge.find_edge_windows(("视频号", "edge"))
        if wins:
            from cleaner import win32bg

            st = win32bg.window_state(wins[0]["hwnd"])
            rec.info("Edge 窗口: hwnd=%s 最小化=%s 遮挡隐藏=%s", st["hwnd"], st["minimized"], st["cloaked"])
        return 0
    finally:
        _finish(rec, cfg, session, keep_shots=args.keep_shots)


def cmd_inspect(args) -> int:
    cfg = load_cfg(args)
    rec = make_recorder(cfg)
    session = None
    try:
        session = open_session(cfg, rec, allow_launch=args.launch)
        page = EdgePage(session, cfg, rec)
        probe = page.probe()
        if probe.get("error"):
            rec.error("探针执行失败: %s", probe["error"])
            return 1

        info = probe.get("info") or {}
        print("=" * 78)
        print(f"URL   : {info.get('url')}")
        print(f"标题  : {info.get('title')}")
        print(f"视口  : {info.get('viewport')}   scrollY={info.get('scrollY')}")
        print(f"滚动容器: {probe.get('scrollRoot')}")
        print("-" * 78)
        print("页面顶部可点文本（用于定位 tab / 垃圾桶入口）：")
        for t in (probe.get("topTabs") or [])[:25]:
            r = t["rect"]
            print(f"   {t['text'][:16]:<18} cls={t['cls'][:34]:<36} @({r['left']},{r['top']}) {r['w']}x{r['h']}")
        print("-" * 78)
        print("页面上的『删除 / 更多』元素：")
        for a in (probe.get("actions") or [])[:20]:
            r = a["rect"]
            print(f"   {a['text']:<6} <{a['tag']}> cls={a['cls'][:30]:<32} @({r['left']},{r['top']}) {r['w']}x{r['h']} cursor={a['cursor']}")
        print("-" * 78)

        items = page.scan_list()
        print(f"列表条目解析结果：{len(items)} 条")
        for i, it in enumerate(items[:12], 1):
            r = it["rect"]
            print(
                f"   {i:>2}. time={it['timeText']!r:<22} title={ (it['title'] or '')[:30]!r:<34}\n"
                f"       card={r['left']},{r['top']} {r['w']}x{r['h']}  action={it['actionText']!r}"
            )
        if not items:
            print("   (0 条 —— 检查页面是否在动态列表，或列表是否已加载)")

        print("-" * 78)
        print("页面文本前 800 字：")
        print((info.get("bodyHead") or "")[:800])
        print("=" * 78)

        rec.op("inspect", url=info.get("url", "")[:120], items=len(items))
        return 0
    finally:
        _finish(rec, cfg, session, keep_shots=args.keep_shots)


def cmd_run(args) -> int:
    cfg = load_cfg(args)
    mode = "dry-run" if (args.dry_run or args.cmd == "dry-run") else "run"

    # 只读模式不需要授权；真正动手的才要求 --limit 或 --yes
    if mode == "run" and not args.yes and not args.limit:
        print("拒绝全量运行：请先用 `--limit N` 做小样本，确认无误后加 `--yes` 跑全量。")
        print(EXAMPLES)
        return 1

    rec = make_recorder(cfg)
    stop = threading.Event()
    install_sigint(stop)
    session = None
    state = State(cfg.state_path, f"{cfg.get('range.start')}..{cfg.get('range.end')}")
    rc = 0
    try:
        session = open_session(cfg, rec, allow_launch=args.launch)
        page = EdgePage(session, cfg, rec)
        if args.no_trash:
            cfg.raw.setdefault("trash", {})["enabled"] = False
        cleaner = Cleaner(cfg, rec, page, state, stop)

        stats = cleaner.execute(
            mode=mode,
            limit=int(args.limit or 0),
            skip_collect=bool(getattr(args, "skip_collect", False)),
        )
        print()
        print("=" * 74)
        print("  " + stats.summary())
        print("=" * 74)
        rec.op("done", **{k: v for k, v in stats.__dict__.items() if k != "started"})
    except LoginRequired as e:
        rec.error("登录失效：%s", e)
        print(f"\n[需要人工] {e}\n  处理完登录后重新运行 `python run.py run --resume` 即可续跑。")
        rc = 2
    except NeedHuman as e:
        rec.error("需要人工介入：%s", e)
        print(f"\n[需要人工] {e}")
        rc = 3
    except StopRequested:
        rec.warn("收到停止信号，已保存断点")
        print("\n[已停止] 断点已保存，下次用 --resume 继续。")
        rc = 130
    except RuntimeError as e:
        rec.error("运行中止：%s", e)
        print(f"\n[已熔断] {e}")
        rc = 4
    except KeyboardInterrupt:
        rec.warn("KeyboardInterrupt")
        rc = 130
    except Exception as e:  # noqa: BLE001
        rec.error("未预期异常：%s", e)
        traceback.print_exc()
        rc = 1
    finally:
        try:
            state.save()
        except Exception:
            pass
        _finish(rec, cfg, session, keep_shots=args.keep_shots)
        print(f"\n日志: {rec.logfile}")
    return rc


def cmd_purge(args) -> int:
    cfg = load_cfg(args)
    n = purge_all_shots(cfg.shot_dir)
    print(f"已删除 {n} 张截图（目录 {cfg.shot_dir}）")
    return 0


def cmd_dump_config(args) -> int:
    cfg = load_cfg(args)
    print(cfg.dump())
    return 0


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="视频号批量删除工具（Edge 后台运行，不抢占键鼠与前台）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
        sp.add_argument("--keep-shots", action="store_true", help="结束后保留截图（默认全部删除）")
        sp.add_argument("--launch", action="store_true", help="调试端口未开时自动启动 Edge（会闪一次窗口）")
        return sp

    common(sub.add_parser("selftest", help="离线自检"))

    common(sub.add_parser("probe", help="连通性与登录态检查"))

    common(sub.add_parser("inspect", help="打印页面结构，用于校准选择器"))

    sp = common(sub.add_parser("dry-run", help="只扫描出清单，不点任何按钮"))
    sp.add_argument("--limit", type=int, default=0, help="只列出前 N 条（0=全部）")
    sp.add_argument("--yes", action="store_true", help="（dry-run 不需要）")

    for name, hlp in (("run", "正式运行"), ("resume", "断点续跑")):
        sp = common(sub.add_parser(name, help=hlp))
        sp.add_argument("--limit", type=int, default=0, help="小样本模式：本轮最多处理 N 条")
        sp.add_argument("--yes", action="store_true", help="确认全量运行")
        sp.add_argument("--dry-run", action="store_true", help="等同 dry-run")
        sp.add_argument("--no-trash", action="store_true", help="本轮不处理垃圾桶")
        sp.add_argument("--skip-collect", action="store_true",
                        help="跳过全量扫描，直接用 state.json 里已有的待处理键（省 5 分钟）")

    common(sub.add_parser("purge-shots", help="清空截图目录"))
    common(sub.add_parser("dump-config", help="打印合并后的完整配置"))
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "selftest": cmd_selftest,
        "probe": cmd_probe,
        "inspect": cmd_inspect,
        "dry-run": cmd_run,
        "run": cmd_run,
        "resume": cmd_run,
        "purge-shots": cmd_purge,
        "dump-config": cmd_dump_config,
    }
    if not hasattr(args, "dry_run"):
        args.dry_run = args.cmd == "dry-run"
    if not hasattr(args, "limit"):
        args.limit = 0
    if not hasattr(args, "yes"):
        args.yes = False
    if not hasattr(args, "no_trash"):
        args.no_trash = False
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
