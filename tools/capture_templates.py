#!/usr/bin/env python3
"""模板图采集工具（可选步骤）。

流程图的 confirm_dialog / delete_menu / success_toast 三张模板不是必须的：
不配模板，程序靠 DOM + 坐标阶梯照样能跑；配了模板，就多一层图像校验。

用法：
    # 1) 在 Edge 里把要采集的画面摆好（例如让确认弹窗停在屏幕上）
    python tools/capture_templates.py                 # 截一张全图存到 templates/
    python tools/capture_templates.py --select        # 截图并弹框框选，存成模板

    # 2) 框选时用鼠标拖出按钮区域，回车确认，ESC 取消
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import cv2  # noqa: E402

from cleaner import cdp, edge, vision  # noqa: E402
from cleaner.config import Config  # noqa: E402
from cleaner.page import EdgePage  # noqa: E402
from cleaner.recorder import Recorder  # noqa: E402


def grab_page(cfg: Config) -> "cv2.Mat | None":
    host, port = cfg.get("cdp.host", "127.0.0.1"), int(cfg.get("cdp.port", 9222))
    if not edge.cdp_alive(host, port):
        print(f"[!] 调试端口 {host}:{port} 未监听，请先运行 start-edge.bat")
        return None
    targets = cdp.list_targets(host, port)
    t = cdp.pick_target(targets, cfg.get("cdp.match_url", ""))
    if t is None:
        print("[!] 没找到视频号页面")
        return None
    rec = Recorder(cfg.log_dir, cfg.shot_dir, "tplcap", verbose=False)
    with cdp.Session(t, timeout=30) as s:
        s.set_active()
        png = s.screenshot()
    img = vision.to_bgr(png)
    if img is None:
        print("[!] 截图解码失败")
    return img


def grab_window() -> "cv2.Mat | None":
    """不依赖调试端口的备用方案：PrintWindow 后台抓 Edge 窗口。"""
    from cleaner import win32bg

    img = win32bg.capture_evidence(("视频号", "Edge"))
    if img is None or vision.is_blank(img):
        print("[!] PrintWindow 抓到的是空白（Edge 有时不给抓），请改用默认的 CDP 截图")
        return None
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description="采集模板图")
    ap.add_argument("-c", "--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--select", action="store_true", help="截完弹框手动框选")
    ap.add_argument("--name", default="", help="保存名（不带扩展名），配合 --select")
    ap.add_argument("--window", action="store_true", help="改用 PrintWindow 抓窗口，不走 CDP")
    args = ap.parse_args()

    cfg = Config.load(Path(args.config))
    tpl_dir = cfg.abspath("templates")
    tpl_dir.mkdir(parents=True, exist_ok=True)

    img = grab_window() if args.window else grab_page(cfg)
    if img is None:
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    full = tpl_dir / f"_screen-{stamp}.png"
    vision.imwrite(full, img)
    print(f"[OK] 全图: {full}  ({img.shape[1]}x{img.shape[0]})")

    if not args.select:
        print("提示：用画图/截图工具裁出按钮区域另存为 templates/confirm_dialog.png 即可。")
        print("      或者加 --select 让脚本弹框框选。")
        return 0

    name = args.name or f"tpl-{stamp}"
    print("[..] 拖出矩形选区，按回车确认，按 ESC 取消")
    time.sleep(0.2)
    roi = cv2.selectROI("select template (ENTER=ok, ESC=cancel)", img, showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()
    x, y, w, h = (int(v) for v in roi)
    if w < 6 or h < 6:
        print("[!] 选区太小，已取消")
        return 1
    crop = img[y : y + h, x : x + w]
    out = tpl_dir / f"{name}.png"
    vision.imwrite(out, crop)
    print(f"[OK] 模板: {out}  ({w}x{h})  位置 ({x},{y})")
    print("     把 config.yaml 里 locate.templates 的路径改成这个文件名即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
