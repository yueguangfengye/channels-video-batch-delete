"""channels-cleaner 核心包。

模块职责：
    config   配置加载（缺项有默认值，不崩）
    dates    发布时间文本解析 / 范围过滤
    state    断点续跑状态机
    recorder 日志与失败截图
    throttle 随机间隔、熔断、标志文件暂停
    edge     Edge 浏览器定位与带调试端口启动
    cdp      Chrome DevTools Protocol 客户端
    dom      页面 DOM 探针（后台列表扫描 / 点击 / 弹窗识别）
    page     把 dom 探针包装成 Python 对象
    vision   模板匹配（坐标兜底通道）
    win32bg  PrintWindow 后台截图 + 窗口枚举（坐标兜底的取图来源）
    locator  多坐标点阶梯定位 + 点击后校验
    flow     业务流程编排
"""

__version__ = "1.0.0"
