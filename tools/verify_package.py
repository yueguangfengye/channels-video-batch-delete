#!/usr/bin/env python3
"""包完整性校验。

在本包根目录运行：
    python tools/verify_package.py

检查：文件完整性、Python 语法、绝对路径残留、敏感数据泄漏、
SKILL frontmatter、配置键一致性、依赖格式、MANIFEST 哈希。
任何一项不通过都会以退出码 1 结束，并给出具体原因。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

# 校验过程本身不要留下 __pycache__，否则会被"敏感产物"检查判为失败
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "SKILL.md", "README.md", "VERSION", "CHANGELOG.md", "metadata.json",
    "requirements.txt", "config.example.yaml",
    "run.py", "daemon.py", "run.bat", "start-edge.bat", "start-daemon.bat",
    "cleaner/__init__.py", "cleaner/config.py", "cleaner/dates.py", "cleaner/state.py",
    "cleaner/recorder.py", "cleaner/throttle.py", "cleaner/restriction.py",
    "cleaner/edge.py", "cleaner/cdp.py", "cleaner/dom.py", "cleaner/page.py",
    "cleaner/locator.py", "cleaner/vision.py", "cleaner/win32bg.py", "cleaner/flow.py",
    "tools/capture_templates.py", "tools/dom_fixture_test.js",
    "templates/README.md",
    "tests/__init__.py", "tests/test_offline.py", "tests/test_restriction_flow.py",
    "docs/INPUT_OUTPUT.md", "docs/SAFETY.md", "docs/TROUBLESHOOTING.md",
    "docs/EXAMPLES.md", "docs/OPERATIONS.md",
]

FORBIDDEN = ["state.json", ".edge-profile", "logs", "shots", "__pycache__", "*.png"]

# 盘符前不能紧跟字母/数字，否则 https:// 里的 "s:" 会被误判成盘符
ABS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`]+")
# 合理的平台探测路径（Windows 标准安装位置），不算硬编码
ALLOWED_HINTS = ("Program Files", "AppData", "LOCALAPPDATA", "PROGRAMFILES", "localappdata")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def check_required() -> list[str]:
    return [f"缺少必需文件：{r}" for r in REQUIRED if not (ROOT / r).exists()]


def check_python_syntax() -> list[str]:
    bad = []
    for f in ROOT.rglob("*.py"):
        if "tests" not in f.parts and "tools" not in f.parts and "cleaner" not in f.parts and f.name not in ("run.py", "daemon.py"):
            continue
        try:
            ast.parse(_read(f))
        except SyntaxError as e:
            bad.append(f"{f.relative_to(ROOT)}: {e}")
    return bad


def check_no_absolute_paths() -> list[str]:
    bad = []
    for ext in ("*.py", "*.bat", "*.yaml", "*.json", "*.js"):
        for f in ROOT.rglob(ext):
            if "docs" in f.parts:
                continue
            for i, line in enumerate(_read(f).splitlines(), 1):
                m = ABS_PATH.search(line)
                if m and not any(h in line for h in ALLOWED_HINTS):
                    bad.append(f"{f.relative_to(ROOT)}:{i} -> {m.group(0)[:60]}")
    return bad


def check_no_sensitive() -> list[str]:
    bad = []
    for pat in FORBIDDEN:
        for f in ROOT.rglob(pat):
            if f.name.endswith(".sha256"):
                continue
            bad.append(f"包含敏感/运行产物：{f.relative_to(ROOT)}")
    return bad


def check_frontmatter() -> list[str]:
    try:
        import yaml
    except ImportError:
        return ["缺少 PyYAML，无法校验 frontmatter"]
    txt = _read(ROOT / "SKILL.md")
    parts = txt.split("---")
    if len(parts) < 3:
        return ["SKILL.md 缺少 frontmatter"]
    try:
        fm = yaml.safe_load(parts[1])
    except Exception as e:
        return [f"frontmatter 解析失败：{e}"]
    missing = [k for k in ("name", "description", "删除程序", "触发条件") if k not in fm]
    return [f"frontmatter 缺少字段：{m}" for m in missing]


def check_config_keys() -> list[str]:
    try:
        import yaml
    except ImportError:
        return ["缺少 PyYAML，无法校验配置键"]
    sys.path.insert(0, str(ROOT))
    from cleaner.config import DEFAULTS

    example = yaml.safe_load(_read(ROOT / "config.example.yaml"))
    if not isinstance(example, dict):
        return ["config.example.yaml 不是对象"]

    def flatten(d, prefix=""):
        out = set()
        for k, v in d.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict):
                out |= flatten(v, key + ".")
            else:
                out.add(key)
        return out

    ex_keys = flatten(example)
    def_keys = flatten(DEFAULTS)
    extra = sorted(ex_keys - def_keys)
    return [f"config.example.yaml 里有 DEFAULTS 没有的键：{k}" for k in extra]


def check_requirements() -> list[str]:
    lines = [l.strip() for l in _read(ROOT / "requirements.txt").splitlines() if l.strip() and not l.startswith("#")]
    bad = []
    for l in lines:
        if not re.match(r"^[a-zA-Z0-9_\-\[\]]+(>=|<=|==|~=|>=|!=|$)", l):
            bad.append(f"requirements.txt 行格式异常：{l}")
    return bad


def check_manifest() -> list[str]:
    mp = ROOT / "MANIFEST.sha256"
    if not mp.exists():
        return ["MANIFEST.sha256 不存在（打包时自动生成，校验前先跑 tools/verify_package.py --write-manifest）"]
    entries = {}
    for line in _read(mp).splitlines():
        line = line.strip()
        if not line:
            continue
        h, _, rel = line.partition("  ")
        entries[rel.strip()] = h.strip()
    bad = []
    for rel, expect in entries.items():
        f = ROOT / rel
        if not f.exists():
            bad.append(f"MANIFEST 里的文件不存在：{rel}")
            continue
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        if actual != expect:
            bad.append(f"哈希不一致：{rel}")
    # 反向：有文件没在 MANIFEST 里
    for f in ROOT.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(ROOT)).replace("\\", "/")
        if rel in ("MANIFEST.sha256",):
            continue
        if rel not in entries:
            bad.append(f"文件未登记到 MANIFEST：{rel}")
    return bad


def write_manifest() -> None:
    lines = []
    for f in sorted(ROOT.rglob("*")):
        if not f.is_file():
            continue
        rel = str(f.relative_to(ROOT)).replace("\\", "/")
        if rel == "MANIFEST.sha256":
            continue
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"{h}  {rel}")
    (ROOT / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"MANIFEST.sha256 已写入，共 {len(lines)} 个文件")


def main() -> int:
    if "--write-manifest" in sys.argv:
        write_manifest()
        return 0

    checks = [
        ("必需文件", check_required),
        ("Python 语法", check_python_syntax),
        ("绝对路径残留", check_no_absolute_paths),
        ("敏感数据泄漏", check_no_sensitive),
        ("SKILL frontmatter", check_frontmatter),
        ("配置键一致性", check_config_keys),
        ("依赖格式", check_requirements),
        ("MANIFEST 一致性", check_manifest),
    ]
    fails = 0
    print("=" * 72)
    print("包完整性校验")
    print("=" * 72)
    for name, fn in checks:
        problems = fn()
        if problems:
            fails += 1
            print(f"  [FAIL] {name}")
            for p in problems[:8]:
                print(f"         - {p}")
            if len(problems) > 8:
                print(f"         ... 还有 {len(problems) - 8} 条")
        else:
            print(f"  [PASS] {name}")
    print("-" * 72)
    print(f"  {len(checks) - fails}/{len(checks)} 通过" + ("" if fails == 0 else f"，{fails} 项不通过"))
    print("=" * 72)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
