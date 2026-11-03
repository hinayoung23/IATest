# iOS Instruments 解析夹具

`process-xcode27.xml` 是此前脱敏的 macOS 进程表结构夹具，不代表 iPhone 采集。

`frames-xcode27-updates.xml` 与 `frames-xcode27-lifetimes.xml` 摘自 2026-10-19 iPhone 15 Pro / iOS 27.2 Beta、Xcode 27.1 的真实 Animation Hitches 录制，保留两条关联记录。PID 替换为 777，引用展开并去掉 ID/fmt；原始数值、表名和工程类型保留。不含设备标识、业务文本或环境变量。对应实际完整样本与验收见 docs/ios-performance-repair.md。
