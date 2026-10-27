# 交付自检表

> 生成时间：2026-10-27　版本：1.0.0

## 一、需求项对照

| # | 交付要求 | 对应文件 | 验证方式 | 结果 |
|---|---|---|---|---|
| 1 | 清晰的目录结构 | 包根 + `cleaner/ tools/ tests/ docs/ templates/` | 见 README「四、目录结构」 | ✅ |
| 2 | Skill 主文件 | `SKILL.md` | frontmatter 四字段齐全、六章齐备 | ✅ |
| 3 | 必要的程序与脚本 | `run.py` `daemon.py` `run.bat` `start-edge.bat` `start-daemon.bat` + `cleaner/` 15 个模块 | `run.py selftest` 9/9 | ✅ |
| 4 | 依赖文件 | `requirements.txt`（4 项，均有版本下限） | `verify_package.py` 依赖格式检查 | ✅ |
| 5 | 资源文件 | `templates/` + 采集工具 `tools/capture_templates.py` | 目录存在、说明齐备 | ✅ |
| 6 | 配置说明 | `config.example.yaml`（逐项中文注释）、`docs/OPERATIONS.md` | 键与 `DEFAULTS` 比对一致 | ✅ |
| 7 | 安装与调用说明 | `README.md`（快速开始 7 步）、`docs/OPERATIONS.md` | 人工review | ✅ |
| 8 | 输入输出规范 | `docs/INPUT_OUTPUT.md` | 三种输入格式 + CSV/JSON/日志格式 | ✅ |
| 9 | 使用示例 | `docs/EXAMPLES.md`（7 个场景） | 人工review | ✅ |
| 10 | 限制与禁止事项 | `docs/SAFETY.md` 第二节（13 条）+ `SKILL.md` 第六章（12 条） | 人工review | ✅ |
| 11 | 安全注意事项 | `docs/SAFETY.md`（6 条）+ `SKILL.md` 第三章（8 条硬性规则） | 人工review | ✅ |
| 12 | 异常处理方式 | `docs/TROUBLESHOOTING.md`（12 行速查表）+ `SKILL.md` 第五章 | 人工review | ✅ |
| 13 | 版本信息 | `VERSION` `CHANGELOG.md` `metadata.json` | 三处版本号一致 = 1.0.0 | ✅ |
| 14 | 校验/测试文件 | `tests/test_offline.py`（13 用例）、`tests/test_restriction_flow.py`、`tools/verify_package.py`、`MANIFEST.sha256` | 全部执行 | ✅ |

## 二、验证记录

### 2.1 离线单元测试

```
python -m unittest tests.test_offline
Ran 13 tests in 0.18s
OK
```

覆盖：时间解析（6 种格式 + 相对时间）、范围边界（含首尾、排除范围外、无法解析不入选）、
配置默认值、状态机往返与损坏恢复、限流日志与报告、节流参数、模板匹配、SKILL frontmatter
与章节、绝对路径残留。

### 2.2 包完整性校验

```
python tools/verify_package.py
8/8 通过
```

| 检查项 | 结果 |
|---|---|
| 必需文件（39 个） | PASS |
| Python 语法 | PASS |
| 绝对路径残留 | PASS |
| 敏感数据泄漏（state.json / .edge-profile / logs / shots） | PASS |
| SKILL frontmatter 四字段 | PASS |
| 配置键一致性（example vs DEFAULTS） | PASS |
| 依赖格式 | PASS |
| MANIFEST 哈希一致性 | PASS |

### 2.3 程序自检

```
python run.py selftest
9/9 通过
```

配置加载、时间解析、范围过滤、断点状态机、模板匹配、窗口枚举、页面探针脚本、
Edge 可执行文件、限制记录机制。

## 三、兼容性说明

| 依赖 | 必需性 | 说明 |
|---|---|---|
| Windows 10/11 | 必需 | `win32bg.py` 用 Win32 API；bat 为 Windows 批处理 |
| Edge / Chrome（Chromium 内核） | 必需 | 需要 CDP 调试通道 |
| Python 3.10+ | 跑程序时必需 | 只用 SKILL.md 不需要 |
| Node.js 18+ | 可选 | 仅离线校验注入 JS 语法 |
| AI 模型 | 不限 | `SKILL.md` 为纯 Markdown 流程，10B 级模型可执行 |

**不通用部分（已在 README 明确标注）**：
`start-edge.bat` / `start-daemon.bat` / `run.bat`、`cleaner/win32bg.py`、浏览器登录态。

## 四、敏感数据确认

包内**不含**：`state.json`（视频标题清单）、`logs/`（操作日志）、`shots/`（截图）、
`.edge-profile/`（登录凭据）、`__pycache__/`、任何 `.png` 截图。

已由 `verify_package.py` 的「敏感数据泄漏」检查自动确认。
