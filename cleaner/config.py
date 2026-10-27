"""配置加载与访问。

设计要点：
1. 内置 DEFAULTS，config.yaml 缺任何一项都不会 KeyError 崩溃；
2. 支持点路径访问 cfg.get("limits.daily_max", 60)；
3. 所有相对路径统一相对「项目根目录」（config.yaml 所在目录）解析。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULTS: Dict[str, Any] = {
    "app": {
        "log_dir": "logs",
        "shot_dir": "shots",
        "state_file": "state.json",
        "purge_shots_on_finish": True,
    },
    "range": {"start": "2022-01-01", "end": "2025-12-31"},
    "cdp": {
        "host": "127.0.0.1",
        "port": 9222,
        "match_url": "channels.weixin.qq.com",
        "timeout": 30,
    },
    "edge": {
        "exe": "",
        "profile_dir": ".edge-profile",
    },
    "urls": {
        "post_list": "https://channels.weixin.qq.com/platform/post/list",
        "trash_url": "",
        "trash_tab_text": ["垃圾桶", "回收站", "已删除"],
    },
    "selectors": {
        "list_root": ["", ".post-list", "#app"],
        "card_max_up": 8,
        "card_min_width": 240,
        "card_min_height": 56,
        "card_max_height": 420,
        "delete_texts": ["删除"],
        "more_texts": ["更多", "…", "...", "管理"],
        "confirm_texts": ["确定", "确认删除", "删除", "确认", "继续删除"],
        "cancel_texts": ["取消", "关闭"],
        "login_signals": ["扫码登录", "请登录", "登录后", "微信扫一扫", "登录视频号"],
        "captcha_signals": ["滑块", "安全验证", "请完成验证", "拖动滑块"],
        "risk_signals": ["操作过于频繁", "系统繁忙", "请稍后再试", "网络异常"],
        "success_signals": ["删除成功", "已删除", "操作成功"],
    },
    "locate": {
        "delete_offsets": [
            {"name": "卡片右上-更多", "anchor": "card_tr", "dx": -28, "dy": 28},
            {"name": "卡片右中", "anchor": "card_tr", "dx": -70, "dy": 0},
            {"name": "卡片右下", "anchor": "card_br", "dx": -28, "dy": -28},
            {"name": "卡片中心偏右", "anchor": "card_center", "dx": 160, "dy": 0},
        ],
        "confirm_offsets": [
            {"name": "弹窗主按钮", "anchor": "dialog_center", "dx": 0, "dy": 34},
            {"name": "弹窗主按钮-左", "anchor": "dialog_center", "dx": -70, "dy": 34},
            {"name": "弹窗主按钮-右", "anchor": "dialog_center", "dx": 70, "dy": 34},
        ],
        "templates": {
            "confirm_dialog": "templates/confirm_dialog.png",
            "delete_menu": "templates/delete_menu.png",
            "success_toast": "templates/success_toast.png",
        },
        "template_threshold": 0.82,
        "click_verify_wait": 0.45,
        "attempts_per_point": 2,
    },
    "limits": {
        "daily_max": 60,
        "max_consecutive_failures": 4,
        "max_items_per_run": 0,
        "min_delay": 1.25,
        "max_delay": 3.75,
        # 长休（每 N 条停 20~45 秒）已整体移除，不再有周期性长休
        "scroll_pause": 1.2,
        "page_wait": 1.6,
        "max_pages": 400,
        "rescan_rounds": 3,
        "max_scroll_rounds": 400,
        "max_idle_scrolls": 5,
        "cooldown_seconds": 0,
        "max_retry_per_item": 2,
        "max_cooldown_rounds": 0,
    },
    "pause": {
        "pause_flag": ".pause",
        "resume_flag": ".resume",
        "auto_resume_after": 0,
        "beep": True,
    },
    "verify": {
        "row_disappear_timeout": 4.0,
        "dialog_timeout": 3.5,
        "shot_on_failure": True,
        "shot_on_success": False,
    },
    "trash": {"enabled": False},
    "restriction": {
        "cooldown_seconds": 0,
        "max_retries": 5,
        "min_page_ratio": 0.9,
        "expected_pages": 0,
        "report_file": "RESTRICTION-REPORT.md",
    },
}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: Dict[str, Any], path: Path | None = None):
        self.raw = _deep_merge(DEFAULTS, data or {})
        self.path = Path(path) if path else None
        self.root = self.path.parent if self.path else Path.cwd()

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path).resolve()
        if not p.exists():
            return cls({}, p)
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data, p)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur

    def abspath(self, rel: str | Path) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.root / p)

    @property
    def log_dir(self) -> Path:
        d = self.abspath(self.get("app.log_dir", "logs"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def shot_dir(self) -> Path:
        d = self.abspath(self.get("app.shot_dir", "shots"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def state_path(self) -> Path:
        return self.abspath(self.get("app.state_file", "state.json"))

    def dump(self) -> str:
        return yaml.safe_dump(self.raw, allow_unicode=True, sort_keys=False)
