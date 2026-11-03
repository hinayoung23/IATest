# 环境与外部工具

Python 基础包不自动下载浏览器、Android SDK、Xcode、Docker 或数据库服务。先安装所需工具，再运行相应 doctor；没有使用的能力不要求准备环境。

| 场景 | 环境与授权 |
|---|---|
| 通用 CLI / 控制台 | Python 3.11+；浏览器访问本机 Web 服务 |
| Chromium | `IATEST_CHROMIUM_PATH` 指向已有可执行文件，使用独立临时配置 |
| Firefox | `IATEST_FIREFOX_PATH` 与 `IATEST_FIREFOX_DRIVER_PATH`，或明确的回环 WebDriver 服务 |
| Safari | macOS、系统 safaridriver；用户启用允许远程自动化；必要时退出普通 Safari 进程 |
| macOS 桌面 | Xcode SDK 编译 AX 驱动；无障碍授权；窗口截图需要 macOS 14+ 与屏幕录制授权 |
| Windows 桌面 | 已登录、解锁的交互桌面；PowerShell 与系统 UIAutomation；SSH 会话本身不等于交互桌面 |
| Linux X11 | AT-SPI、GIO、X11/XComposite 与可用无障碍总线 |
| GNOME Wayland | AT-SPI、GIO、GdkPixbuf、RemoteDesktop 门户、GNOME 组件；用户确认系统输入及单应用窗口授权 |
| Android | SDK platform/build-tools/platform-tools、JDK 17+、USB 调试及授权 ADB |
| iOS | macOS/Xcode、匹配 SDK、设备信任/开发者模式、已有开发团队和签名 Runner |
| 独立 iOS TLS | 手机与控制端局域网互通、Runner 本地网络权限、明确证书与设备绑定 |
| Android VM / 容器 | 对应模拟器系统镜像或预装框架的 Docker 镜像；由控制器持有实例生命周期 |
| 网络数据库 | `iatest-framework[database]`；数据库地址、TLS、只读账号和允许表 |
| HID 中文输入 | `iatest-framework[hid]`，或调用方显式提供拼音；输入法及候选结果可观察 |
| gRPC 描述符 | 已有 protoc 或 `IATEST_PROTOC`；运行时使用冻结的描述符 |
| Android AAB/APKS | Java、bundletool、apksigner、已有业务签名引用和目标设备规格 |
| 原生 H.264 解码 | 支持 WebCodecs 的浏览器，或控制端已有 FFmpeg |
| 远程节点 | 局域网、TLS、节点身份和能力允许列表；高可用协调另需 etcd |

## 检查入口

```sh
iatest test capabilities
iatest test doctor --browser chromium
iatest test doctor --browser webkit --network-policy navigation_only
iatest mobile doctor
iatest mobile perf-doctor
```

`capabilities` 是实现能力；doctor 的运行验证与设备/App 业务验证分别判断。成功发现设备或浏览器文件不代表业务测试通过。

## 权限与秘密

模型、数据库、节点与业务凭据使用已存在的环境变量引用。不要把密码、私钥、个人浏览器配置或手机配对记录写入计划。证书、App 重签、卸载、手机重启及蓝牙重启属于显式操作，框架不自动替用户授权。

控制台保留本机回环绑定。生产代理、证书和身份部署由操作者配置；测试证书、开发签名和本地资源路径不能作为正式发布配置。
