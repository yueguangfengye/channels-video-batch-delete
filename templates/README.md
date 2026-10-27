# templates/ 目录说明

这里放**模板图**（可选），用于 DOM 定位完全失效时的图像兜底。

## 什么时候需要

正常情况下程序按 DOM 节点定位按钮，不需要任何模板。只有当视频号后台**页面结构大改**、DOM 探针完全失效时，模板匹配才会作为最后一级兜底启用。

## 需要的三张模板（可选）

| 文件 | 内容 | 采集方法 |
|---|---|---|
| `confirm_dialog.png` | 删除确认弹窗 | 用 `python tools/capture_templates.py --select` 框选 |
| `delete_menu.png` | 浮层菜单里的「删除」项 | 同上 |
| `success_toast.png` | 顶部「已删除」提示 | 同上 |

## 不配模板会怎样

不影响运行。只是少了图像校验这一层，DOM + 坐标阶梯已经足够。

## 采集步骤

1. 在 Edge 里把要采集的画面摆好（例如让确认弹窗停在屏幕上）
2. 运行 `python tools/capture_templates.py --select`
3. 用鼠标拖出按钮区域，回车确认
4. 文件保存后，把 `config.yaml` 里 `locate.templates.*` 的路径指到它

不配模板时，`locate.templates.*` 指向不存在的文件即可，程序会自动跳过。
