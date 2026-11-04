# IATest 文档

IATest 将 API、浏览器、桌面、移动端、性能、安全与数据协议测试连接成可重复执行的业务流程。本文档描述当前实现、使用方式和明确的能力边界。

## 开始使用

- [安装与第一个项目](guides/getting-started.md)
- [环境与外部工具](reference/environment.md)
- [能力与平台边界](reference/capabilities.md)
- [Web 控制台](guides/web-console.md)

## 功能指南

| 要完成的工作 | 文档 |
|---|---|
| 导入接口、生成并执行用例 | [API 自动化](guides/api-testing.md) |
| 浏览器输入、frame、Shadow DOM、Safari | [浏览器测试](guides/browser-testing.md) |
| Mac、Windows、Linux 与 GNOME Wayland | [桌面测试](guides/desktop-testing.md) |
| Android 与 iOS 原生计划、设备连接 | [移动端测试](guides/mobile-testing.md) |
| 视频、手势、IME、局部视觉与共享资源 | [移动高级能力](guides/mobile-advanced.md) |
| 使用安卓手机作为 iPhone 键鼠 | [蓝牙 HID](guides/bluetooth-hid.md) |
| 外接 USB HID 桥接 | [USB HID](guides/usb-hid.md) |
| 目标探索、真实图像模型与证据分析 | [AI 目标探索](guides/goal-exploration.md) |
| 类型化计划与跨类型工作流 | [多类型测试](guides/test-plans.md) |
| 数据库、WebSocket、MQTT 与 gRPC | [数据与协议](guides/data-protocol.md) |
| 负载、客户端指标、安全和无障碍 | [性能与安全](guides/performance-security.md) |
| iOS 采集、校准和离线门禁 | [iOS 性能](guides/ios-performance.md) |
| 用例资产、安装交付、版本测试任务 | [业务测试](guides/business-testing.md) |
| 数据生成、Agent、配方、MCP 和 Token | [运行时数据与 Agent](guides/agent-data.md) |
| 执行进程、容器及访问控制 | [执行隔离](guides/sandbox.md) |
| 节点证书、授权和撤销 | [节点运维](guides/node-operations.md) |

## 规范与设计

- [用例格式](reference/case-format.md)
- [判定与证据规范](reference/quality.md)
- [原生引擎依据](reference/native-engines.md)
- [开发方案](design/development-plan.md)
- [总体架构](design/architecture.md)
- [上下文与知识治理](design/context.md)
- [控制台视觉与交互规范](design/web-console.md)

文档按主题维护：指南说明如何使用，规范说明契约和边界，设计说明实现机制。日常更新直接修订对应章节，不追加开发轮次、临时测试计数或设备调试流水。正式版本变更保留在 [2.0.1 版本与迁移说明](releases/2.0.1.md)。
