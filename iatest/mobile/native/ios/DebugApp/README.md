# IATest Lab

默认可执行名为 lab，与 bundle ID 尾段对齐，以适配当前 XCTest application 卡顿采集器的名称筛选；App 包仍为 IATestDebugApp.app、显示名仍为 IATest Lab。修正仅针对本调试 App，不为业务 App 改名，详见[实际对照](../../../../../docs/guides/ios-performance.md)。

用于 iOS 真机验收的框架自有 UIKit App，独立于 XCTest Runner，不需要业务 App 或生产账号。提供昵称保存/重启读回、开关、列表/详情导航，以及滑动/缩放反馈。数据仅写入自己的沙盒；Reset lab 只清理该 App 的昵称和界面状态。Debug 构建还发布已有只读 bridge 的就绪、控件 ID 和输入状态；这些提示不能替代真实 UI 断言。

项目和 Runner 都需要有效开发签名及该设备可用的描述文件。先在 Xcode → Settings → Apple Accounts 登录自己的开发账号。未签名构建只能证明编译通过，不能安装或作为真机验收结果。

从仓库根目录构建，替换团队和设备占位值：

```bash
xcodebuild build \
  -project iatest/mobile/native/ios/DebugApp/IATestDebugApp.xcodeproj \
  -scheme IATestDebugApp -configuration Debug \
  -destination 'platform=iOS,id=YOUR_UDID' \
  -derivedDataPath native/mobile/build/ios-lab \
  DEVELOPMENT_TEAM=YOUR_TEAM

xcodebuild build-for-testing \
  -project iatest/mobile/native/ios/IATestMobile.xcodeproj \
  -scheme IATestMobile -destination 'platform=iOS,id=YOUR_UDID' \
  -derivedDataPath native/mobile/build/ios-runner \
  DEVELOPMENT_TEAM=YOUR_TEAM
```

描述文件不存在时，Xcode 可在用户授权后使用 `-allowProvisioningUpdates -allowProvisioningDeviceRegistration` 获取签名资产并登记设备。构建脚本不保存团队账号或私钥。

安装签名后的 App，Runner 由框架会话启动：

```bash
xcrun devicectl device install app --device YOUR_UDID \
  native/mobile/build/ios-lab/Build/Products/Debug-iphoneos/IATestDebugApp.app

python scripts/validate_ios_device.py \
  --device YOUR_UDID --app dev.iatest.mobile.lab \
  --xctestrun /path/to/signed/runner.xctestrun \
  --output /private/tmp/ios-device-validation.json \
  --exercise-lab --media
```

省略 `--exercise-lab` 时只检查已经在前台的目标 App，不派发 App 输入。该选项只接受本 App 的包名。脚本没有合成设备替代，不调用模型；截图在内存检查，报告只存元数据。`--media` 取 H.264 编码帧及宿主解码帧，不衡量持续帧率。输入测试会保存计划及真实运行报告，失败或未知派发后不自动重试。

Debug Lab 还提供受控滚动校准：`iatest mobile ios-calibrate` 通过当次启动环境启用/关闭每次 3×250ms 的主线程阻塞，并读取 `ready_label` 的 stalls 计数验证刺激。平时启动不启用，未写持久开关；Release 编译排除注入开关、计数器和阻塞调用。请用 `iatest mobile build ios --device --team YOUR_TEAM --performance` 构建独立的 Release 性能 Runner，再更新 profile 的 xctestrun。方案与范围限制见[性能说明](../../../../../docs/guides/ios-performance.md)。
