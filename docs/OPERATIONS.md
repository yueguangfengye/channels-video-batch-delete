# 视频号批量删除工具（Edge 后台运行版）

在 Windows 上批量删除视频号（channels.weixin.qq.com）中 **2022-01-01 ~ 2025-12-31** 发布的视频，
全程在后台运行：**不移动系统光标、不抢窗口焦点、不影响你正常用电脑**。

---

## 先读这三条

1. **运行期间不要手动去操作那个 Edge 窗口**。程序的点击是按页面坐标派发的，
   你如果同时在里面滚动或点击，会把它点歪。想用电脑做别的事完全没问题——
   但请把那个 Edge 窗口最小化或留在后台，别去碰它。
2. **Edge 窗口可以最小化、可以被别的窗口盖住，但不能关闭**，也不能退出登录。
3. **第一次必须先跑 `start-edge.bat`** 并扫码登录一次。之后登录态存在独立 profile 里，长期有效。

---

## 一、为什么不用"窗口句柄 + 坐标点击"

你原来的思路是后台截图找按钮、算坐标、发消息点击。这条路在 Chromium（Edge）上有个硬伤，
我实测确认过：

| 通道 | 能否后台 | 能否点击 Edge | 说明 |
|---|---|---|---|
| `PostMessage(WM_LBUTTONDOWN)` | 能 | **基本不能** | Edge 渲染进程不认合成消息，点了没反应 |
| `mouse_event` / `SendInput` | 不能 | 能 | 要移动真实光标、要把窗口切到前台，直接违反"不抢前台" |
| **CDP `Input.dispatchMouseEvent`** | **能** | **能** | 派发真实输入事件，不需要焦点，不动光标 |
| **CDP `Page.captureScreenshot`** | **能** | — | 窗口被遮挡也能截图，是真正的"后台截图" |
| `PrintWindow(PW_RENDERFULLCONTENT)` | 能 | — | 通常能抓到画面，留作备用取证 |

所以本工具的主通道是 **CDP（Chrome DevTools Protocol）**。顺带解决你提到的核心痛点：

> "垃圾桶中的删除按钮像素位置会随标题长度变化"

因为按钮是**按 DOM 节点找的**，不是按像素找的。找到节点后取它的包围盒中心再点击，
标题多长都不影响。坐标点仍然保留了，作为 DOM 失效时的第 4 级兜底。

---

## 二、目录结构

```
channels-cleaner/
├─ run.py                    # 入口（所有命令）
├─ config.yaml               # 全部配置
├─ start-edge.bat            # 启动带调试端口的 Edge（独立 profile）
├─ requirements.txt
├─ cleaner/
│  ├─ config.py              # 配置加载（缺项有默认值）
│  ├─ dates.py               # 发布时间解析 / 范围过滤
│  ├─ state.py               # 断点续跑状态机
│  ├─ recorder.py            # 日志 + 失败截图
│  ├─ throttle.py            # 随机间隔 / 熔断 / 标志文件暂停
│  ├─ edge.py                # Edge 定位、启动、窗口枚举
│  ├─ cdp.py                 # CDP 客户端 + 鼠标派发
│  ├─ dom.py                 # 注入页面的探针 JS
│  ├─ page.py                # 探针的 Python 封装
│  ├─ vision.py              # 模板匹配
│  ├─ win32bg.py             # PrintWindow 后台截图（备用取证）
│  ├─ locator.py             # 四级定位 + 点击校验
│  └─ flow.py                # 业务流程编排
├─ tools/
│  ├─ capture_templates.py   # 采集模板图（可选）
│  └─ dom_fixture_test.js    # 扫描算法离线测试
├─ templates/                # 模板图（可选）
├─ logs/                     # 日志
└─ shots/                    # 失败截图（运行结束自动清空）
```

---

## 三、快速开始

```bat
:: 0. 确认依赖（本机已全有，一般跳过）
pip install -r requirements.txt

:: 1. 启动带调试端口的 Edge（会自动弹一个干净的 Edge 窗口）
start-edge.bat
::    → 在这个窗口里扫码登录视频号，登录后保持窗口开着（可最小化）

:: 2. 连通性 + 登录态检查
python run.py probe

:: 3. 看页面结构解析得对不对（第一次务必看）
python run.py inspect

:: 4. 出待删清单，人工核对（不点任何按钮）
python run.py dry-run

:: 5. 小样本试删 3 条
python run.py run --limit 3

:: 6. 确认无误，正式全量
python run.py run --yes
```

`dry-run` 会把清单打印到控制台，并写一份 CSV：

```
logs/dryrun-20261027-141230.csv
序号,发布日期,是否在范围,标题,时间原文,key
1,2022-03-05,是,实体店老板必看：客流下滑的真正原因,2022-03-05 12:30,9f2a...
```

---

## 四、命令一览

| 命令 | 作用 | 安全性 |
|---|---|---|
| `selftest` | 离线自检（配置、日期解析、状态机、模板匹配、窗口枚举、JS 语法） | 只读 |
| `probe` | 检查调试端口、登录态、当前视口能识别几条 | 只读 |
| `inspect` | 打印页面 tab / 删除按钮 / 列表条目 / 滚动容器，用于校准 | 只读 |
| `dry-run` | 滚动遍历列表，出待删清单 + CSV | 只读 |
| `run --limit N` | 小样本：本轮最多处理 N 条 | 会真删 |
| `run --yes` | 全量 | 会真删 |
| `resume` | 断点续跑（等价于 run，会跳过已完成项） | 会真删 |
| `purge-shots` | 清空 shots 目录 | 只删图 |
| `dump-config` | 打印合并后的完整配置（含默认值） | 只读 |

通用参数：`-c 配置文件`、`--keep-shots`（结束后保留截图）、`--launch`（端口没开时自动拉起 Edge）。

---

## 五、配置说明

全部在 `config.yaml`，改完直接重跑，不需要重新安装。

### 5.1 时间范围

```yaml
range:
  start: "2022-01-01"
  end: "2025-12-31"      # 闭区间，含首尾
```

支持四种时间文本，都能正确解析：`2022-03-05`、`2022/3/5 12:30`、`2022年3月5日`、`2022.03.05`。
相对时间（`3天前`、`昨天 18:20`）按系统当天倒推，不在范围内就跳过。

### 5.2 按钮坐标点（多组备用）

```yaml
locate:
  delete_offsets:                       # 列表页/垃圾桶的"删除/更多"入口
    - { name: "卡片右上-更多", anchor: "card_tr",     dx: -24, dy:  24 }
    - { name: "卡片右中",      anchor: "card_tr",     dx: -60, dy:   0 }
    - { name: "卡片右下",      anchor: "card_br",     dx: -24, dy: -24 }
    - { name: "卡片右上-内缩", anchor: "card_tr",     dx: -48, dy:  44 }
    - { name: "卡片中心偏右",  anchor: "card_center", dx: 180, dy:   0 }

  confirm_offsets:                      # 确认弹窗的"确定"按钮
    - { name: "弹窗底部中",      anchor: "dialog_bc", dx:   0, dy: -36 }
    - { name: "弹窗底部中-偏右", anchor: "dialog_bc", dx:  64, dy: -36 }
    - { name: "弹窗底部中-偏左", anchor: "dialog_bc", dx: -64, dy: -36 }
    - { name: "弹窗中心偏下",    anchor: "dialog_center", dx: 0, dy: 48 }
```

**锚点**（`anchor`）不是绝对像素，是相对位置，所以窗口大小/DPI 变了也不用改：

| 锚点 | 含义 |
|---|---|
| `card_tl` / `card_tr` / `card_bl` / `card_br` | 卡片左上 / 右上 / 左下 / 右下 |
| `card_center` | 卡片中心 |
| `dialog_bc` / `dialog_tc` / `dialog_center` | 弹窗底部中点 / 顶部中点 / 中心 |

**`dx` 右为正，`dy` 下为正，单位是 CSS 像素。**

坐标点是**逐个尝试**的：点一个 → 等 450ms → 检查确认弹窗是否出现 →
没出现就点下一个。所以点歪了不会误删，只会继续试下一个点。

需要加自己的坐标点时，用 `run.py inspect` 看卡片的实际尺寸，再算偏移。

### 5.3 延时与节流

```yaml
limits:
  min_delay: 1.25         # 两条视频之间的最小间隔（秒）
  max_delay: 3.75         # 最大间隔，实际取两者之间的随机值
  cooldown_seconds: 0     # 失败后等待秒数，0=不等待（长休已移除）
  scroll_pause: 1.2       # 每滚一屏等多久渲染
```

### 5.4 安全上限

```yaml
limits:
  daily_max: 60                    # 单日最多处理多少条，跨天自动重置（0=不限）
  max_consecutive_failures: 4      # 连续失败 4 次 → 熔断停止
  max_scroll_rounds: 400           # 最多滚 400 屏
  max_idle_scrolls: 5              # 连续 5 屏无新条目 → 认为到底

verify:
  row_disappear_timeout: 8.0       # 点确认后等条目消失的时限
  dialog_timeout: 3.5              # 等确认弹窗出现的时限
  shot_on_failure: true            # 失败时截图
```

### 5.5 截图策略（对应你的第 6 条要求）

```yaml
app:
  purge_shots_on_finish: true    # 运行完毕后删除所有截图
```

- 运行中：失败会往 `shots/<runid>/` 存图（带 `index.jsonl` 索引）。
- **运行结束时（不论是跑完、中断还是熔断），整个 `shots/` 目录会被清空。**
- 清空前会把"第几张 / 什么标签 / 哪个视频 / 什么原因"逐条写进 `logs/run-<runid>.log`，
  所以排查信息不会丢，只是不留图片。
- 想留图：改 `purge_shots_on_finish: false`，或运行时加 `--keep-shots`。

---

## 六、测试步骤（建议按这个顺序）

### 第 0 级：离线自检（不连网、不碰浏览器）

```bat
python run.py selftest
```

9 项检查：配置、日期解析、范围过滤、状态机、模板匹配、窗口枚举、JS 语法完整性、Edge 路径、端口状态。

### 第 1 级：连通性

```bat
python run.py probe
```

应看到 `登录态：正常` 和 `当前视口可识别条目：N 条`。

如果显示 `登录态：失效`，回到 Edge 窗口扫码。

### 第 2 级：结构校准

```bat
python run.py inspect
```

重点看三块输出：

1. **页面顶部可点文本** —— 里面应该有"垃圾桶 / 回收站 / 已删除"，没有的话说明
   `urls.trash_tab_text` 要改，或者填 `urls.trash_url`。
2. **页面上的『删除 / 更多』元素** —— 确认能找到，且 `cursor=pointer`。
3. **列表条目解析结果** —— 每条应该显示正确的 `time=` 和 `title=`。
   如果是 0 条或时间全是 `?`，说明页面结构变了，需要调 `selectors`。

### 第 3 级：清单核对（零风险）

```bat
python run.py dry-run
```

打开生成的 CSV，**逐条核对**发布日期和标题。这一步不点任何按钮，随便跑。

### 第 4 级：小样本

```bat
python run.py run --limit 3
```

删 3 条，看日志：

```
[delete_begin] phase=list title=实体店老板必看... time=2022-11-03
[click_target] how=dom-action text=删除
[confirm_click] how=dom text=确定
[delete_ok] phase=list how=dom-action confirm=dom
```

`how=dom-action` / `confirm=dom` 说明走的是最可靠的 DOM 通道。
如果变成 `how=coordinate`，说明 DOM 没命中，去 `logs/` 里看 `ops.jsonl` 找原因。

### 第 5 级：全量

```bat
python run.py run --yes
```

---

## 七、日志与失败截图

| 文件 | 内容 |
|---|---|
| `logs/run-<runid>.log` | 人类可读，每步一行，含时间戳 |
| `logs/ops.jsonl` | 结构化，一行一个 JSON，便于统计/复盘 |
| `shots/<runid>/*.png` | 失败截图（结束时清空） |
| `shots/<runid>/index.jsonl` | 截图索引：序号、标签、备注 |
| `state.json` | 断点状态 |

`ops.jsonl` 里每一步都有 `action` 字段，常用值：

```
scan_round      每滚一屏的记录：visible / new / total
scan_end        扫描结束及原因
click_target    点了什么（how=dom-action | dom-text | hover-rescan | coordinate | menu-item）
confirm_click   确认弹窗怎么点的
delete_ok       成功
delete_fail     失败（tag=no_dialog | no_confirm | still_present）
signal          检测到登录失效 / 验证码 / 风控
pause / resume  暂停与恢复
throttle        每次随机间隔的秒数
```

---

## 八、安全机制

### 8.1 暂停 / 中断（不用碰控制台）

在**项目根目录**建/删这些文件即可，全程不抢你的键盘：

| 文件 | 效果 |
|---|---|
| 建 `.pause` | 下一次检查点即暂停 |
| 建 `.resume` | 从暂停恢复（`.pause` 会被自动删掉） |
| 建 `.stop` | 优雅退出，保存断点 |

遇到验证码 / 风控提示时，程序会**自己写 `.pause` 并响一声**，然后阻塞等待。
你在 Edge 里手动处理完，建一个 `.resume` 就继续：

```bat
type nul > .resume
```

（也可以直接在资源管理器里新建一个空文件改名成 `.resume`）

### 8.2 断点续跑

每条视频处理完都会写 `state.json`。中断后重新运行：

```bat
python run.py resume --yes
```

已标记为 `deleted` / `trashed` / `skipped` 的会自动跳过。

### 8.3 熔断

连续失败 4 次（默认）直接停，不硬撑。避免页面改版后一路瞎点。

### 8.4 不会发生的事

- 不会点"取消"以外的按钮去关弹窗（浮层用 ESC 关，不会误触）
- 不会在没检测到确认弹窗的情况下盲点确认键
- 不会删除范围外的视频（时间解析失败的一律视为不在范围，跳过并在清单里标"无法解析"）
- 不会把 `state.json` 写坏（原子写：临时文件 + rename）

---

## 九、被内容管理限制时怎么办（自动恢复）

限制发生时不会静默失败，也不会盲目重试。完整流程是：
**记录现场 → 立即探测是否恢复 → 通过则继续，不通过则再探测一次 → 5 轮仍失败则转人工。**

### 9.1 记录了什么

每次限制写一条结构化记录到 `logs/restrictions.jsonl`，同时刷新人类可读的
`logs/RESTRICTION-REPORT.md`。

| 字段 | 含义 |
|---|---|
| `ts` | 限制发生的时间点，精确到秒 |
| `signal` | 页面命中的原文，例如「操作过于频繁」 |
| `rule` | 触发的规则条目，例如 `selectors.risk_signals ← 命中 '操作过于频繁'` |
| `phase` / `page_no` | 发生在哪个阶段（预检/list/trash）、第几页 |
| `current_item` | 当时正在删除哪条视频 |
| `url` / `page_text` | 页面地址，以及限制提示的原文（截取 1200 字） |
| `video_count` / `pager_total` / `items_visible` | 页面视频总数 / 总页数 / 当前页条目数 |
| `deleted_today` | 限制发生时今日已删多少条 |
| `cooldown_seconds` / `retry_index` / `max_retries` | 本次等待时长、第几次重试、上限 |

报告分三段：限制明细表、每次的现场上下文、恢复探测记录。

### 9.2 时间线与重试上限

| 项目 | 默认值 | 配置项 |
|---|---|---|
| 每次等待 | **0 秒（不等待）** | `restriction.cooldown_seconds` |
| 最多重试 | **5 次** | `restriction.max_retries` |

第 N 轮：记录 → 立即探测 → 通过则继续执行未完成的任务；不通过进入第 N+1 轮。
不等待期间随时可用 `.stop` / Ctrl+C 中止。

### 9.3 每次重试前必须同时满足三个条件

1. **无限制提示**：页面上不再出现任何风险词或验证码词
2. **列表可用**：能正常扫到条目，数量 > 0
3. **页数未缩水**：分页总页数 ≥ 预期的 90%
   （专门针对「总页数从 138 被压成 10」这种隐性限流，它不报错但会漏数据）

三条全过才算恢复；任一不过就立刻进入下一次探测。

### 9.4 5 次都失败时的处理

不硬撑、不空转：

1. 生成 `logs/RESTRICTION-REPORT.md`，含全部现场与探测记录
2. 写 `.pause` 文件（内容是限制原因 + 报告路径）
3. 停手退出，**退出码 4**
4. 断点全部保留在 `state.json`

人工确认平台已解禁后，直接续跑，不用重新扫描：

```bat
python run.py run --yes --skip-collect
```

### 9.5 和「普通删除失败」的区别

| 场景 | 等待 | 重试上限 | 配置项 |
|---|---|---|---|
| 内容管理限制 / 数据被截断 | 不等待 | 5 次 | `restriction.*` |
| 单条视频定位失败（弹窗没出来等） | 不等待 | 2 次 | `limits.cooldown_seconds` / `limits.max_retry_per_item` |

---

## 十、常见问题

**Q: `start-edge.bat` 跑完端口还是不通**
说明你本机已经有 Edge 在跑，新进程把参数转交给老实例了。
**把所有的 Edge 窗口全部关掉**，再跑一次。

**Q: `probe` 报 `连不上 Edge 调试端口`**
同 Q1。或者 `config.yaml` 里的 `cdp.port` 和 `start-edge.bat` 里的 `PORT` 不一致。

**Q: `inspect` 显示 0 条**
1. 确认页面停在"动态/内容管理"列表页，不是首页；
2. 手动滚一下让列表加载出来，再跑一次；
3. 如果还是 0，把 `inspect` 输出的"页面文本前 800 字"发我，说明页面结构变了。

**Q: 日志里 `how=coordinate` 很多**
DOM 没命中，通常原因是按钮要 hover 才出现。可以在 `config.yaml` 里把
`locate.click_verify_wait` 调到 `0.8`，或者把 `selectors.more_texts` 补上页面上的实际文案。

**Q: 找不到垃圾桶入口**
先 `inspect` 看"页面顶部可点文本"里垃圾桶叫什么，改 `urls.trash_tab_text`；
或者手动进垃圾桶，把地址栏 URL 填进 `urls.trash_url`。

**Q: 跑到一半 Edge 崩了**
`python run.py resume --yes` 继续，已处理的项不会重做。

**Q: 提示"达到单日上限"**
改 `limits.daily_max`，或等第二天（计数按自然日重置）。

---

## 十一、需求对照

| 你的要求 | 实现位置 | 怎么验证 |
|---|---|---|
| 1. 后台执行，不抢键鼠和前台 | 全流程走 CDP：`Input.dispatchMouseEvent` 派发输入、`Page.captureScreenshot` 后台截图，无任何 `SetForegroundWindow` / `SetCursorPos` | 运行期间随便打字、拖窗口，不会被抢 |
| 2. 遍历列表（滚动加载）→ 按时间筛选 → 逐个删除 → 进垃圾桶彻底删除 | `flow.collect` / `flow.run_delete_loop` / `flow.run_trash_phase` | `dry-run` 出清单；`run --limit 3` 验证链路 |
| 3. 多组备用坐标 + 模板匹配 + 动态定位 + 点击后校验 + 失败切换下一坐标 | `locator.py` 四级阶梯（DOM → 卡片内文本 → hover 重扫 → 坐标点），每级都校验弹窗；`vision.py` 做模板匹配 | 日志里的 `how=` 字段 |
| 4. dry-run / 小样本 / 随机间隔 / 单日上限 / 登录失效·验证码·异常弹窗暂停 / 中断与断点续跑 | `run.py dry-run`、`--limit`、`throttle.py`、`state.py`、`flow.guard`、`.pause`/`.resume`/`.stop` | 见第六、八节 |
| 5. 完整代码 + 配置说明 + 测试步骤 + 每步日志 + 失败截图 | 本文件 + `config.yaml` 注释 + `logs/run-*.log` + `ops.jsonl` + `shots/` | — |
| 6. 运行完毕后删除所有截图 | `app.purge_shots_on_finish: true`，在 `finally` 里执行，清空整个 `shots/` | 跑完看 `shots/` 是否为空 |

---

## 十二、已知限制

1. **DOM 探针必须对着真实页面校准一次**。我按视频号后台的常见结构写了启发式算法
   （找最内层日期文本 → 向上归位卡片 → 提取标题 → 找操作按钮），并在离线 DOM 沙箱里
   验证过 4 种时间格式都能正确识别。但**真实页面结构如果大改，需要你跑 `inspect`
   把输出发我，我再调选择器**。这是唯一无法离线验证的部分。
2. **首次必须扫码登录**。独立 profile 是为了让调试端口能起来，代价是登录态要重建一次。
3. **不要并发跑多个实例**。同一个页面被两个程序同时操作必然乱。
4. 本工具只删除**你自己账号**下、**在配置时间范围内**的视频，请自行确认清单后再全量执行。

---

## 附：离线验证命令（开发者用）

```bat
:: 1) 导出注入页面的 JS 并校验语法
python -c "import json;from cleaner import dom;open('_jscheck.json','w',encoding='utf-8').write(json.dumps({'lib':dom.JS_LIB},ensure_ascii=False))"
node -e "new Function(require('fs').readFileSync('_jscheck.json','utf8').lib); console.log('JS SYNTAX OK')"

:: 2) 在离线 DOM 沙箱里跑扫描算法（4 条 fixture，检查时间/标题/按钮识别）
node tools/dom_fixture_test.js _jscheck.json
```
