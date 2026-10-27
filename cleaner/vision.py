"""模板匹配（坐标兜底通道的取图与识别）。

只依赖 opencv + numpy。中文路径用 np.fromfile 读，避免 cv2.imread 在
Windows 上对非 ASCII 路径返回 None。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np


@dataclass
class Match:
    x: int  # 左上
    y: int
    w: int
    h: int
    score: float

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def __str__(self) -> str:
        return f"({self.cx},{self.cy}) score={self.score:.3f}"


def imread(path: str | Path) -> Optional[np.ndarray]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        buf = np.fromfile(str(p), dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None


def imwrite(path: str | Path, img: np.ndarray) -> bool:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        ok, buf = cv2.imencode(p.suffix or ".png", img)
        if not ok:
            return False
        buf.tofile(str(p))
        return True
    except Exception:
        return False


def to_bgr(png_bytes: bytes) -> Optional[np.ndarray]:
    if not png_bytes:
        return None
    try:
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def find_all(
    screen: np.ndarray,
    tpl: np.ndarray,
    threshold: float = 0.82,
    scales: Sequence[float] = (1.0,),
) -> List[Match]:
    """多尺度模板匹配，返回所有 score >= threshold 的位置。"""
    if screen is None or tpl is None or screen.size == 0 or tpl.size == 0:
        return []
    sh, sw = screen.shape[:2]
    out: List[Match] = []
    gray_s = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY) if screen.ndim == 3 else screen
    gray_t = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY) if tpl.ndim == 3 else tpl

    # 模板几乎是纯色时 TM_CCOEFF_NORMED 会退化（分母趋零，位置随机），
    # 这种情况改用 TM_SQDIFF_NORMED 并把分数翻转成"越大越像"。
    low_var = float(gray_t.std()) < 3.0

    for sc in scales:
        if abs(sc - 1.0) < 1e-6:
            t = gray_t
        else:
            t = cv2.resize(gray_t, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        th, tw = t.shape[:2]
        if th > sh or tw > sw or th < 6 or tw < 6:
            continue
        method = cv2.TM_SQDIFF_NORMED if low_var else cv2.TM_CCOEFF_NORMED
        res = cv2.matchTemplate(gray_s, t, method)
        score = (1.0 - res) if low_var else res
        score = np.nan_to_num(score, nan=0.0, posinf=0.0, neginf=0.0)
        ys, xs = np.where(score >= threshold)
        for y, x in zip(ys, xs):
            out.append(Match(int(x), int(y), int(tw), int(th), float(score[y, x])))
    out.sort(key=lambda m: -m.score)
    return out


def best(
    screen: np.ndarray,
    tpl: np.ndarray,
    threshold: float = 0.82,
    scales: Sequence[float] = (1.0,),
) -> Optional[Match]:
    hits = find_all(screen, tpl, threshold, scales)
    return hits[0] if hits else None


def is_blank(img: Optional[np.ndarray], tol: float = 2.0) -> bool:
    """判断整幅图是否近乎全黑（PrintWindow 抓 Chromium 失败时的典型表现）。"""
    if img is None or img.size == 0:
        return True
    return float(img.std()) < tol and float(img.mean()) < 12.0


def crop(img: np.ndarray, rect: Tuple[int, int, int, int]) -> Optional[np.ndarray]:
    x, y, w, h = rect
    ih, iw = img.shape[:2]
    x2, y2 = min(iw, x + w), min(ih, y + h)
    x, y = max(0, x), max(0, y)
    if x2 <= x or y2 <= y:
        return None
    return img[y:y2, x:x2]
