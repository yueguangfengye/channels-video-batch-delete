"""发布时间文本解析与时间范围过滤。

视频号列表里的发布时间格式不统一，实测至少出现过：
    2022-03-05
    2022/03/05 12:30
    2022年3月5日
    2022.03.05
    3天前 / 2小时前 / 昨天 / 前天 / 刚刚
本模块把上述文本统一解析成 date 对象，供范围过滤使用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

# 绝对日期：年-月-日，分隔符兼容 - / . 年月日
_ABS_DATE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")

# 相对时间
_REL_DAYS = re.compile(r"(\d{1,3})\s*天前")
_REL_HOURS = re.compile(r"(\d{1,3})\s*小时前")
_REL_MINS = re.compile(r"(\d{1,3})\s*分钟前")
_WORD_TODAY = re.compile(r"刚刚|今天")
_WORD_YESTERDAY = re.compile(r"昨天")
_WORD_2DAYS = re.compile(r"前天")

# 只有"月日"没有年份（视频号在"当年"发布时常这样显示）
_ABS_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)")


@dataclass
class ParsedTime:
    """解析结果。"""

    value: Optional[date]  # 解析出的日期；None 表示无法解析
    kind: str  # 'abs' 绝对 / 'rel' 相对 / 'md' 仅月日 / 'none' 失败
    raw: str  # 原始文本片段

    def __str__(self) -> str:
        if self.value is None:
            return f"<{self.kind}:{self.raw}>"
        return self.value.isoformat()


def parse_publish_time(text: str, today: Optional[date] = None) -> ParsedTime:
    """从任意包含时间信息的文本中解析出发布日期。

    解析优先级：绝对日期 > 相对时间 > 仅月日。
    """
    if not text:
        return ParsedTime(None, "none", "")
    today = today or date.today()
    txt = str(text)

    m = _ABS_DATE.search(txt)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return ParsedTime(date(y, mo, d), "abs", m.group(0))
        except ValueError:
            return ParsedTime(None, "none", m.group(0))

    m = _REL_DAYS.search(txt)
    if m:
        return ParsedTime(today - timedelta(days=int(m.group(1))), "rel", m.group(0))
    if _WORD_TODAY.search(txt):
        return ParsedTime(today, "rel", "今天")
    if _WORD_YESTERDAY.search(txt):
        return ParsedTime(today - timedelta(days=1), "rel", "昨天")
    if _WORD_2DAYS.search(txt):
        return ParsedTime(today - timedelta(days=2), "rel", "前天")
    m = _REL_HOURS.search(txt) or _REL_MINS.search(txt)
    if m:
        return ParsedTime(today, "rel", m.group(0))

    # 仅月日：按当年推断。若推断结果晚于今天，则归属上一年。
    m = _ABS_MD.search(txt)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        for year in (today.year, today.year - 1):
            try:
                cand = date(year, mo, d)
            except ValueError:
                continue
            if cand <= today:
                return ParsedTime(cand, "md", m.group(0))
    return ParsedTime(None, "none", txt[:32])


def in_range(d: Optional[date], start: date, end: date) -> bool:
    """日期是否落在闭区间 [start, end] 内。None 一律视为不在范围内。"""
    if d is None:
        return False
    return start <= d <= end


def parse_bound(text: str) -> date:
    """解析配置里的边界日期字符串，如 '2022-01-01'。"""
    text = str(text).strip()
    m = _ABS_DATE.search(text)
    if not m:
        raise ValueError(f"无法解析日期边界: {text!r}，应形如 2022-01-01")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def humanize(d: Optional[date]) -> str:
    return d.isoformat() if d else "-"
