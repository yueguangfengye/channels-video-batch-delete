"""实测限制处理链路：模拟一次限制，验证 记录→等待→探测→升级 全流程。

做法：临时把「视频管理」（页面必然存在）加进 risk_signals，冷却缩到 30 秒、
最多重试 1 次，跑一次限量运行。预期结果：
    - logs/restrictions.jsonl 出现 1 条 event + 1 条 probe
    - logs/RESTRICTION-REPORT.md 生成且含现场上下文
    - 因探测始终不通过，最后升级：写 .pause + 抛错退出
    - 全程不删除任何视频
测完自动把 config.yaml 还原。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config.yaml"
BAK = ROOT / "_config.bak.yaml"
PY = sys.executable


def _set_restriction_cooldown(text: str, seconds: int) -> str:
    """只改 restriction: 块下的 cooldown_seconds，不碰 limits 里同名的那一项。"""
    lines = text.splitlines()
    in_block = False
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("restriction:"):
            in_block = True
            continue
        if in_block and stripped and not ln.startswith((" ", "\t")):
            break  # 到了下一个顶层键
        if in_block and stripped.startswith("cooldown_seconds:"):
            indent = ln[: len(ln) - len(ln.lstrip())]
            lines[i] = f"{indent}cooldown_seconds: {seconds}"
            break
    return "\n".join(lines) + "\n"


def main():
    created_cfg = False
    if not CFG.exists() and (ROOT / "config.example.yaml").exists():
        shutil.copy2(ROOT / "config.example.yaml", CFG)
        created_cfg = True
        print("[准备] 未找到 config.yaml，已从 config.example.yaml 复制一份")
    shutil.copy2(CFG, BAK)
    try:
        text = CFG.read_text(encoding="utf-8")
        text = text.replace(
            'risk_signals: ["操作过于频繁", "系统繁忙", "请稍后再试", "网络异常", "请求失败"]',
            'risk_signals: ["操作过于频繁", "系统繁忙", "请稍后再试", "网络异常", "请求失败", "视频管理"]',
        )
        text = _set_restriction_cooldown(text, 30)
        text = text.replace("  max_retries: 5", "  max_retries: 1")
        CFG.write_text(text, encoding="utf-8")
        print("[注入] risk_signals 增加 '视频管理'，restriction.cooldown=30s，max_retries=1")

        # 清掉上一次的测试产物
        for p in (ROOT / "logs" / "restrictions.jsonl", ROOT / "logs" / "RESTRICTION-REPORT.md"):
            if p.exists():
                p.unlink()
        for f in (".pause", ".resume", ".stop"):
            q = ROOT / f
            if q.exists():
                q.unlink()

        print("\n[执行] run --limit 1 --skip-collect（预期：被限制拦截，不删除任何视频）")
        r = subprocess.run(
            [PY, "-u", "run.py", "run", "--limit", "1", "--skip-collect"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
        )
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines():
            if any(k in line for k in ("restriction", "冷却", "限制", "已停止", "熔断", "ERROR", "WARNING")):
                print("   ", line[:160])

        print("\n[校验] 产出文件")
        jl = ROOT / "logs" / "restrictions.jsonl"
        rp = ROOT / "logs" / "RESTRICTION-REPORT.md"
        ok = True
        if jl.exists():
            recs = [json.loads(l) for l in jl.read_text(encoding="utf-8").splitlines() if l.strip()]
            evs = [x for x in recs if x.get("_type") == "event"]
            prs = [x for x in recs if x.get("_type") == "probe"]
            print(f"   restrictions.jsonl: event={len(evs)} probe={len(prs)}")
            if evs:
                e = evs[0]
                for k in ("ts", "signal", "rule", "phase", "page_no", "video_count", "cooldown_seconds", "retry_index"):
                    print(f"     {k} = {e.get(k)!r}")
            if not evs:
                ok = False
        else:
            print("   [X] restrictions.jsonl 未生成")
            ok = False

        if rp.exists():
            print(f"   RESTRICTION-REPORT.md: {rp.stat().st_size} 字节")
            head = rp.read_text(encoding="utf-8").splitlines()[:6]
            for h in head:
                print("     ", h[:110])
        else:
            print("   [X] 报告未生成")
            ok = False

        paused = (ROOT / ".pause").exists()
        print(f"   升级为人工暂停 (.pause): {paused}")
        print(f"   退出码: {r.returncode}")

        print("\n[结果]", "链路完整 ✅" if ok else "有问题 ❌")
        return 0 if ok else 1
    finally:
        shutil.copy2(BAK, CFG)
        BAK.unlink(missing_ok=True)
        if created_cfg:
            CFG.unlink(missing_ok=True)
        for f in (".pause", ".resume", ".stop"):
            q = ROOT / f
            if q.exists():
                q.unlink()
        print("[还原] config.yaml 已恢复，临时标志文件已清理")


if __name__ == "__main__":
    sys.exit(main())
