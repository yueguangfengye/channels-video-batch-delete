"""离线单元测试：不连网、不开浏览器、不依赖真实页面。

跑法：
    python -m unittest tests.test_offline -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestDates(unittest.TestCase):
    def test_abs_formats(self):
        from cleaner.dates import parse_publish_time

        cases = {
            "2022-03-05": "2022-03-05",
            "2022/3/5 12:30": "2022-03-05",
            "2022年3月5日": "2022-03-05",
            "2022.03.05 08:00": "2022-03-05",
            "发布于 2023年11月9日 21:04": "2023-11-09",
            "2025-12-31": "2025-12-31",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                got = parse_publish_time(src).value
                self.assertIsNotNone(got, src)
                self.assertEqual(got.isoformat(), want)

    def test_rel_dates(self):
        from datetime import date

        from cleaner.dates import parse_publish_time

        got = parse_publish_time("3天前", today=date(2026, 10, 27)).value
        self.assertEqual(got.isoformat(), "2026-10-24")
        self.assertEqual(parse_publish_time("昨天", today=date(2026, 10, 27)).value.isoformat(), "2026-10-26")

    def test_range_boundary(self):
        from cleaner.dates import in_range, parse_bound

        a, b = parse_bound("2022-01-01"), parse_bound("2025-12-31")
        self.assertTrue(in_range(parse_bound("2022-01-01"), a, b), "起始边界应包含")
        self.assertTrue(in_range(parse_bound("2025-12-31"), a, b), "结束边界应包含")
        self.assertFalse(in_range(parse_bound("2021-12-31"), a, b))
        self.assertFalse(in_range(parse_bound("2026-01-01"), a, b))
        self.assertFalse(in_range(None, a, b), "无法解析的日期不得落入范围")

    def test_bad_bound(self):
        from cleaner.dates import parse_bound

        with self.assertRaises(ValueError):
            parse_bound("去年")


class TestConfig(unittest.TestCase):
    def test_defaults_present(self):
        from cleaner.config import Config

        c = Config({})
        self.assertEqual(c.get("range.start"), "2022-01-01")
        self.assertEqual(c.get("range.end"), "2025-12-31")
        self.assertEqual(c.get("limits.min_delay"), 1.25)
        self.assertEqual(c.get("limits.max_delay"), 3.75)
        self.assertEqual(c.get("restriction.cooldown_seconds"), 3600)
        self.assertEqual(c.get("restriction.max_retries"), 5)
        self.assertFalse(c.get("trash.enabled"))

    def test_dot_path(self):
        from cleaner.config import Config

        c = Config({"a": {"b": {"c": 42}}})
        self.assertEqual(c.get("a.b.c"), 42)
        self.assertEqual(c.get("a.b.missing", "fallback"), "fallback")


class TestState(unittest.TestCase):
    def test_roundtrip_and_status(self):
        from cleaner.state import State

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            st = State(p, "2022-01-01..2025-12-31")
            st.upsert("k1", "标题A", "2022-04-03 17:30", "list")
            st.mark("k1", "deleted")
            st.bump("deleted_today")
            st.save()

            st2 = State(p, "2022-01-01..2025-12-31")
            self.assertEqual(st2.status_of("k1"), "deleted")
            self.assertEqual(st2.stats["deleted_today"], 1)
            self.assertEqual(st2.pending(), [])

    def test_corrupt_file_recovered(self):
        from cleaner.state import State

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            p.write_text("{broken json", encoding="utf-8")
            st = State(p)
            self.assertEqual(st.targets, {}, "损坏文件应回退到空状态而不是崩溃")


class TestRestrictionLog(unittest.TestCase):
    def test_record_and_report(self):
        from cleaner.restriction import ProbeResult, RestrictionEvent, RestrictionLog

        with tempfile.TemporaryDirectory() as td:
            log = RestrictionLog(Path(td))
            log.record(
                RestrictionEvent(
                    ts="2026-10-27 16:00:00",
                    kind="content_management_restriction",
                    signal="操作过于频繁",
                    rule="selectors.risk_signals ← 命中",
                    phase="list",
                    page_no=12,
                    video_count=2732,
                )
            )
            log.record(ProbeResult(ts="2026-10-27 17:00:00", retry_index=1, recovered=False, reason="仍受限"))
            s = log.summary()
            self.assertEqual(s["events"], 1)
            self.assertEqual(s["probes"], 1)
            self.assertTrue(log.report_path.exists())
            txt = log.report_path.read_text(encoding="utf-8")
            self.assertIn("操作过于频繁", txt)
            self.assertIn("触发规则", txt)
            self.assertIn("视频数", txt)


class TestThrottle(unittest.TestCase):
    def test_bounds_and_default(self):
        from cleaner.config import Config
        from cleaner.throttle import Throttle

        class R:
            def op(self, *a, **k): pass
            def debug(self, *a, **k): pass
            def warn(self, *a, **k): pass

        t = Throttle(Config({}), R())
        self.assertEqual((t._lo, t._hi), (1.25, 3.75))
        self.assertEqual(t._every, 12)
        self.assertGreaterEqual(t._lmax, t._lmin)


class TestVision(unittest.TestCase):
    def test_template_match(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy 不可用")

        from cleaner import vision

        screen = np.zeros((200, 320, 3), dtype=np.uint8)
        screen[:] = (30, 30, 30)
        tpl = np.zeros((24, 60, 3), dtype=np.uint8)
        tpl[:] = (240, 240, 240)
        tpl[::4, :] = (20, 20, 20)
        screen[100:124, 150:210] = tpl
        m = vision.best(screen, tpl, threshold=0.9)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m.cx, 180, delta=3)
        self.assertAlmostEqual(m.cy, 112, delta=3)
        self.assertFalse(vision.is_blank(screen))
        self.assertTrue(vision.is_blank(np.zeros((40, 40, 3), dtype=np.uint8)))


class TestSkillDoc(unittest.TestCase):
    def test_frontmatter_and_sections(self):
        txt = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        parts = txt.split("---")
        self.assertGreaterEqual(len(parts), 3, "SKILL.md 缺少 frontmatter")

        try:
            import yaml

            fm = yaml.safe_load(parts[1])
        except ImportError:
            self.skipTest("PyYAML 不可用")

        for key in ("name", "description", "删除程序", "触发条件"):
            self.assertIn(key, fm, f"frontmatter 缺少 {key}")

        for section in (
            "角色与目标", "输入规范", "强制安全规则", "分步执行流程",
            "异常处理速查表", "禁止行为清单",
        ):
            self.assertIn(section, txt, f"SKILL.md 缺少章节 {section}")

        self.assertIn("确认删除", txt)
        self.assertIn("页面不匹配", txt)
        self.assertIn("opr-item-wrap", txt)


class TestNoAbsolutePaths(unittest.TestCase):
    def test_no_hardcoded_paths(self):
        """除 docs/OPERATIONS.md 里的目录名示例外，不得出现任何绝对路径。"""
        import re

        # 盘符前不能紧跟字母/数字，否则 https:// 里的 "s:" 会被误判成盘符
        pat = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`]+")
        offenders = []
        for ext in ("*.py", "*.bat", "*.yaml", "*.json"):
            for f in ROOT.rglob(ext):
                if "docs" in f.parts:
                    continue
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    m = pat.search(line)
                    if m and "Program Files" not in line and "AppData" not in line:
                        offenders.append(f"{f.relative_to(ROOT)}:{i}: {m.group(0)[:60]}")
        self.assertEqual(offenders, [], "残留绝对路径：" + "; ".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)
