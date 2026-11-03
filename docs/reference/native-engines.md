# 原生引擎依据与维护

IATest 的生产执行使用自有协议客户端和平台适配器。第三方框架可作为行为研究或开发验证工具，不等于将其作为生产后端，也不据此宣称达到全部能力或性能等价。

| 平台与机制 | 依据 | 维护关注 |
|---|---|---|
| Chromium | [CDP Target](https://chromedevtools.github.io/devtools-protocol/tot/Target/)、[DOM](https://chromedevtools.github.io/devtools-protocol/tot/DOM/)、[Input](https://chromedevtools.github.io/devtools-protocol/tot/Input/) | 会话、对象身份、AX、frame 几何、命中、代理与事件输入 |
| Firefox / Safari | [W3C WebDriver](https://www.w3.org/TR/webdriver2/) | 原生元素引用、frame、Shadow Root 与后端能力；不能假定所有端点等价 |
| GNOME Wayland | [XDG RemoteDesktop](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html) | 系统授权、撤销、显示器坐标；组件只读取获准的应用窗口 |
| Android | Android SDK、ADB、Accessibility、InputConnection、Bluetooth HID/GATT | 系统权限、进程身份、报告描述符、输入语义与前台要求 |
| iOS | Xcode/XCTest、WebKit、受信任设备及签名 Runner | SDK、设备版本、采集质量及输入权限；内部 ABI 需要持续校验 |
| gRPC / MQTT | [gRPC](https://grpc.io/docs/what-is-grpc/core-concepts/)、[MQTT 5.0](https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html) | 流、状态、消息边界、确认、属性与未知投递 |
| 资源协调 | [etcd API](https://etcd.io/docs/v3.6/learning/api/) | 集群身份、线性化、租约、fencing 和失去多数派 |
| 无障碍 | [WCAG 2.2](https://www.w3.org/TR/WCAG22/) | 自动规则与人工复核的范围、来源和构建绑定 |

升级平台、浏览器或工具链时，应重跑相应原生用例和权限负向场景，核对协议/SDK 差异。比较性能、延迟或 Token 时使用明确的相同工作负载和统计方法，不能把合成样例或字符估算当作通用节省比例。

源码的 `scripts/validate_*.py` 提供分场景复验入口；设备、业务服务、权限和签名由操作者明确提供。测试跳过不代表该环境通过，编译成功不等于真实输入成功。
