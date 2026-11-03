# Android 与 iOS 原生测试

## 准备组件

Android 需要 SDK、build-tools、platform-tools、JDK 17+ 和已授权 ADB。框架使用 shell/app_process 输入组件及独立探针，不要求 Appium、Gradle 或业务 App 常驻调试插件。

```sh
iatest mobile build android --sdk /path/to/android-sdk --java-home /path/to/jdk
```

输出包含输入 JAR、探针 APK 与摘要清单。框架开发签名不作为业务发布签名；升级先核对身份，不把覆盖安装失败自动转换为卸载。

iOS 需要 Xcode 与匹配 SDK。模拟器和真机分别构建：

```sh
iatest mobile build ios
iatest mobile build ios --device --team YOUR_TEAM
```

真机使用已有开发团队、受信任签名、开发者模式和匹配的 `.xctestrun`。框架不自动创建证书、绕过设备信任或接管个人 App 数据。

## 控制台流程

1. 在「设备与环境 → 设备配置」检测设备并保存 profile，指定平台、设备和目标 App；iOS 配置已签名 Runner。
2. 打开会话，在观察器按 ID、名称、文本、容器或业务锚点查询目标。
3. 需要交互时显式取得手动控制。发送回执与测试结果分开显示；归还控制后再运行独立计划。
4. 采集明确截图或短视频预览。隐藏标签页停止预览；预览图像不会自动发送给模型。
5. 在「测试工作台 → 界面与交互」选择 Android 或 iPhone/iPad，填写业务目标和通过条件，开始 AI 探索及独立回放；已有步骤可切换「手工编排步骤」。目标用例和计划统一保存在「测试资产 → UI 测试」。通过统一引擎执行的计划在类型化结果查看，独立移动会话运行在移动端结果查看。

## CLI

```sh
iatest mobile doctor
iatest mobile import-profile examples/devices/android.json --project demo
iatest mobile import-plan examples/plans/android-login.json --project demo
iatest mobile run android-login --project demo
iatest mobile reports --project demo
```

profile 中的设备 ID、App ID 和目标标识均需按自己的设备替换。检测到设备不代表驱动、权限和业务 App 已就绪。

## 计划契约

计划包含 `contract_version/id/kind/spec.profile/spec.steps` 和可选预算；`kind` 为 `android` 或 `ios`。最后一次改变状态之后必须有明确预期。

| 操作 | 语义 |
|---|---|
| `find` | 结构化目标；bind 保存重新定位的配方，不保留跨运行句柄 |
| `tap/swipe/pinch` | 唯一且新鲜的目标和几何；手势需实际平台能力 |
| `text` | 目标、值和显式输入模式；UTF-8 不超过 16 KiB |
| `key` | 平台支持的按键；iOS 原生 Runner 的普通 key 入口为 Home |
| `expect/wait` | 明确属性和有限超时；轮询不重放输入 |
| `screenshot` | 有界 PNG 证据 |
| `launch/terminate` | 受能力预检约束的 iOS App 生命周期操作 |
| `fragment` | 有限设备本地 tap/text/expect 片段，末步为断言 |

Android 使用完整资源 ID，iOS 优先 accessibility identifier。容器和 anchor 缩小范围；bounds 按平台的像素或屏幕点解释，不能互换。输入前重新查询，截断结果或占位文字不能证明成功。

Android `semantic` 是 Accessibility 赋值；`key_events` 是聚焦后的真实按键；IME 是独立能力。iOS `keyboard` 使用 XCTest 键盘，语言和输入法行为需观察。中文候选和 HID 输入见[蓝牙 HID](bluetooth-hid.md)。

## 观察、媒体与混合页面

控制帧绑定会话 token、序列、驱动代次和操作 ID。USB、独立 TLS 与媒体通道有各自认证和预算。独立 TLS 需要局域网、本地网络权限及已核对的证书/设备身份，不依赖输入走 USB。

Android 支持原生 WebView、逻辑显示、权限和显式 App 转场。iOS WKWebView 需要 inspectable、仅回环监听的 WebKit 检查器和真实 App/PID/原生容器绑定；输入仍由原生 Runner 执行。支持视口缩放、嵌套 frame、平面透视和独立 CSS 变换，身份不符或遮挡时拒绝。

连续 H.264、局部视觉、节点缓存、多触点、IME、开发构建提示和共享租约见[移动高级能力](mobile-advanced.md)。App 提示只用于定位辅助，不授权业务动作。

## 指标与隔离

Android 可读取进程 CPU/PSS、UID 网络计数及未充电条件下的系统估算电量。iOS 支持显式采集、独立帧区间、启动测量、注册 Metal 区间和离线门禁，限制见[iOS 性能](ios-performance.md)。

物理手机不声明完整 OS 隔离。独立 Android VM 由控制器创建并持有生命周期；root UID 防火墙只限制目标 UID 的显式出口，不等于整机沙箱。

任何连接状态、发送回执或截图都不能代替业务断言。未知投递停止并保留未知状态；取消尝试释放自己的资源，不能回滚 App 已完成的业务操作。
