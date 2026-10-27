# 视频号助手批量删除视频（Skill 交付包）

在电脑端视频号助手（`channels.weixin.qq.com`）后台，按你给定的时间范围批量删除历史视频，
全程**后台运行**——不抢占鼠标、键盘和前台窗口。

---

## 一、这是什么

这是一份「可被 AI Agent 直接加载执行的 Skill」，外加一套已实测可用的参考实现。

| 组件 | 面向谁 | 内容 |
|---|---|---|
| `SKILL.md` | **AI Agent / 大模型** | 纯流程文档：输入规范、强制安全规则、分步判定标准、异常速查表。任何模型都可加载，不依赖特定平台 |
| `cleaner/` + `run.py` + `daemon.py` | **人类部署者** | 参考实现（Python + CDP），已在真实账号上跑通 1192+ 条删除 |

**你可以只用 `SKILL.md`**（把它交给你的 Agent，让它按流程操作），也可以用附带程序直接跑。

---

## 二、兼容性矩阵

| 依赖 | 是否必需 | 说明 |
|---|---|---|
| Windows 10 / 11 | **必需** | 程序用了 Win32 后台截图（`win32bg.py`），bat 启动器也是 Windows 批处理 |
| Edge 或 Chrome | **必需** | 需要 Chromium 系浏览器，通过 `--remote-debugging-port` 提供 CDP 调试通道 |
| Python 3.10+ | **必需**（仅跑程序时） | 只用 SKILL.md 不需要 |
| Node.js | 可选 | 仅用于离线校验注入页面的 JS 语法 |
| AI 模型 | 不限 | SKILL.md 是纯 Markdown 流程，10B 级小模型也能理解执行 |

**明确不通用、不可移植的部分**：

- `start-edge.bat` / `start-daemon.bat` / `run.bat`：仅 Windows。
- `cleaner/win32bg.py`（PrintWindow 后台截图）：仅 Windows，且只是备用取证通道，删掉不影响主流程。
- 浏览器登录态需要本机已登录本人视频号账号，无法跨机器迁移。

macOS / Linux 要复用时：保留 `cleaner/cdp.py`、`dom.py`、`dates.py`、`state.py`、`throttle.py`、`restriction.py` 这些纯逻辑模块，自行替换浏览器启动方式即可。

---

## 三、快速开始

### 3.1 用 Agent 加载（推荐）

把 `SKILL.md` 交给你的 AI Agent，然后说：

> 按这份 Skill，删除 2022-01-01 到 2025-12-31 发布的全部视频。

Agent 会：列清单 → 等你回复「确认删除」→ 分批删除 → 复核。

### 3.2 用附带程序直接跑（Windows）

```bat
:: 1) 安装依赖
pip install -r requirements.txt

:: 2) 复制并修改配置（时间范围、间隔等）
copy config.example.yaml config.yaml

:: 3) 启动带调试端口的 Edge（独立 profile，扫码登录一次）
start-edge.bat

:: 4) 自检与连通性
run.bat selftest
run.bat probe

:: 5) 出清单（不删任何东西）
run.bat dry-run

:: 6) 小样本（删 3 条验证）
run.bat run --limit 3

:: 7) 全量（跑到完成为止，限流自动等待恢复）
start-daemon.bat
```

### 3.3 只用程序、不用 Agent

程序本身就是完整工具，`SKILL.md` 只是它遵循的流程规范。

---

## 四、目录结构

```
channels-video-batch-delete/
├── SKILL.md                  # Skill 主文件（AI Agent 加载这个）
├── README.md                 # 本文件（人类部署者看这个）
├── VERSION                   # 版本号
├── CHANGELOG.md              # 变更记录
├── metadata.json             # 包元信息（版本、依赖、平台）
├── MANIFEST.sha256           # 文件完整性哈希清单
├── PACKAGE-CHECKLIST.md      # 交付自检表
├── requirements.txt          # Python 依赖
├── config.example.yaml       # 配置示例（复制为 config.yaml 使用）
│
├── run.py                    # 单次执行入口
├── daemon.py                 # 守护执行入口（断点续跑、限流自愈）
├── run.bat                   # Windows 便捷启动（自动找 Python）
├── start-edge.bat            # 启动带调试端口的 Edge（独立 profile）
├── start-daemon.bat          # 启动守护模式
│
├── cleaner/                  # 核心库
│   ├── config.py             # 配置加载（缺项有默认值）
│   ├── dates.py              # 发布时间解析与范围过滤
│   ├── state.py              # 断点续跑状态机
│   ├── recorder.py           # 日志与失败截图
│   ├── throttle.py           # 随机间隔、熔断、配置热重载
│   ├── restriction.py        # 限流识别、恢复探测、报告（默认不等待）
│   ├── edge.py               # Edge 定位与带调试端口启动
│   ├── cdp.py                # Chrome DevTools Protocol 客户端
│   ├── dom.py                # 注入页面的探针（支持无界微前端 Shadow DOM）
│   ├── page.py               # 探针封装、页码跳转、滚动
│   ├── locator.py            # 按钮定位四级阶梯 + 点击校验
│   ├── vision.py             # 模板匹配（坐标兜底）
│   ├── win32bg.py            # Win32 后台截图（备用取证，仅 Windows）
│   └── flow.py               # 业务流程编排
│
├── tools/
│   ├── capture_templates.py  # 采集模板图（可选）
│   ├── dom_fixture_test.js   # 离线 DOM 沙箱测试
│   └── verify_package.py     # 包完整性校验
│
├── templates/                # 模板图资源目录（可选，见 README）
│
├── tests/
│   ├── test_offline.py       # 离线单元测试（不连网、不开浏览器）
│   ├── test_restriction_flow.py  # 限流链路集成测试
│   └── fixtures/             # 测试夹具
│
└── docs/
    ├── INPUT_OUTPUT.md       # 输入输出规范
    ├── SAFETY.md             # 安全注意事项与禁止事项
    ├── TROUBLESHOOTING.md    # 异常处理
    ├── EXAMPLES.md           # 使用示例
    └── OPERATIONS.md         # 部署运维手册
```

---

## 五、各文档指引

| 你要做的事 | 看这个 |
|---|---|
| 部署并跑起来 | 本文件（README） |
| 让 AI Agent 理解怎么操作 | `SKILL.md` |
| 输入时间格式 / 输出清单格式 | `docs/INPUT_OUTPUT.md` |
| 安全红线、禁止事项 | `docs/SAFETY.md` |
| 出错了怎么办 | `docs/TROUBLESHOOTING.md` |
| 参考命令示例 | `docs/EXAMPLES.md` |
| 部署运维、调参 | `docs/OPERATIONS.md` |
| 改配置 | `config.example.yaml`（复制为 `config.yaml`） |

---

## 六、版本与更新

当前版本见 `VERSION`。每次变更记录在 `CHANGELOG.md`。

完整性校验：解压后运行 `python tools/verify_package.py`，应输出全部通过。
