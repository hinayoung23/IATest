# 用现有安卓手机发送 iPhone 蓝牙键鼠输入

此方案复用现有 Mac、Android 手机和 iPhone，不需要树莓派或 USB gadget 桥接板。Mac 通过授权 ADB 控制框架自有的安卓 App，安卓以蓝牙键盘/鼠标身份连接 iPhone。输入通道不调用 XCTest；控件查询、截图和业务断言可继续使用 iPhone Runner 的独立 TLS 观察通道。

## 准备与连接

- 安卓需要蓝牙硬件及相应系统支持。经典蓝牙使用 Android 9 起公开的 `BluetoothHidDevice`；BLE 模式使用系统 GATT 服务与外围广播接口。系统版本符合不代表具体厂商手机已经通过验收。
- 安卓连接 Mac 的授权 ADB；桥接 App 保持前台。运行中会占用这台安卓手机的输入设备租约，不能同时拿它运行其它前台 Android 测试。
- iPhone 保持解锁，开启辅助触控；BLE 配对优先从「设置 → 辅助功能/无障碍 → 触控 → 辅助触控 → 设备 → 蓝牙设备」选择安卓。BLE 广播不需要开启安卓的经典蓝牙限时发现。鼠标按钮的动作在辅助触控的设备设置中配置。
- 使用业务断言时，Mac 与 iPhone 需要局域网互通，保留已签名 Runner、本地网络授权及独立 TLS 配置。蓝牙发送本身不要求 iPhone 插 USB。

官方接口见 [Android BluetoothHidDevice](https://developer.android.com/reference/android/bluetooth/BluetoothHidDevice)、[Android GATT Server](https://developer.android.com/reference/android/bluetooth/BluetoothGattServer)；iPhone 设置见 [Apple 指针设备说明](https://support.apple.com/en-us/111775)。

## 构建和安装发送端

使用现有 Android SDK/JDK 和已有的框架开发签名密钥，不下载新的生产依赖。HID 源码编译需要已安装的 Android SDK platform 36 或更新版本；编译版本不等于运行时最低版本：

```bash
iatest mobile build android --hid --keystore /path/to/existing-development.keystore --java-home /path/to/jdk
adb -s ANDROID_SERIAL install -r native/mobile/build/android-hid/iatest-bluetooth-hid.apk
adb -s ANDROID_SERIAL shell am start -W -n dev.iatest.mobile.hid/.MainActivity --es transport ble
```

在安卓系统弹窗中授权附近设备访问。App 会显示实际 HID 注册回调和连接状态；不能仅凭系统中存在 HID 服务判定已可用。示例显式选择 BLE。

需要经典蓝牙时显式启动：

```bash
adb -s ANDROID_SERIAL shell am start -W -n dev.iatest.mobile.hid/.MainActivity --es transport classic
```

经典模式通过 App 的“允许 iPhone 发现”按钮打开限时发现请求，由用户同意并配对。BLE 自动广播 HID 服务，由 iPhone 配对并订阅报告。连接配置省略 `transport` 时仍使用经典模式，应按示例明确填写。模式改变不自动回退；活动或未知输入会话不允许被另一模式接管。同一进程切换模式保留 BLE 服务表和句柄。服务表或报告描述符升级后，iPhone 可能需要首次重新配对；这不能算自动恢复通过。重新打开桥接 App 后会有界尝试恢复并检查新订阅。默认完整重建仍可能被系统缓存阻塞；显式启用安卓蓝牙重启的恢复策略已通过完整服务重建和手机重启后的受控恢复，见[完整服务恢复](#可选完整服务恢复)。

## 可选 ADB 托管 BLE

普通 App 模式遇到安卓蓝牙关闭会丢弃原 GATT 和经典代理，清除注册状态；蓝牙重新开启、界面位于前台时重新注册服务。活动输入会话被标为需要确认，不能沿用原会话继续派发。

显式 `lifecycle: adb_managed` 将 GATT 注册放在有限期的授权 ADB shell 进程中。安卓界面被强停时仍保留同一服务；界面不在前台、退出或心跳超过三秒时禁止输入并尝试释放按键。重新打开界面后由 Mac 重新绑定，不能把残留状态文件当成存活或输入成功。普通 `app` 生命周期仍随 App 进程结束。

此模式需要 Android 12 及以上，并依赖设备实际提供的 shell 蓝牙接口和权限；接口不支持时明确失败。先安装同一构建产物的 APK，再创建连接文件：

```json
{
  "android_device_id": "ANDROID_SERIAL",
  "host_address": "AA:BB:CC:DD:EE:FF",
  "transport": "ble",
  "lifecycle": "adb_managed"
}
```

```bash
iatest mobile bluetooth-hid-owner-start connection.json --artifact-dir native/mobile/build/android-hid --lifetime-seconds 3600
iatest mobile bluetooth-hid-owner-status connection.json
iatest mobile bluetooth-hid-owner-stop connection.json
```

托管期限为 5–7200 秒；重复启动已匹配的实例不会延长期限。状态查询不打开安卓界面，且不把连接和订阅标为业务通过。显式停止或到期会释放报告并移除所属 GATT 服务；独立看门狗在清理卡住时终止自身进程。APK 升级、安卓蓝牙重启、手机重启和托管进程本身退出均会结束原服务连续性，需要重新初始化，不能算作界面强停恢复。

输入配置或 iOS profile 中必须保留同一 `lifecycle`；开启输入会话会通过授权 ADB 重新发布 Binder 绑定。界面与托管端核对 App UID、PID、运行代次、APK 哈希和目标配对；管理通道只允许状态、绑定发布和停止，不接受键鼠输入。没有手机 TCP 监听，也不修改 SELinux。退出托管后，如需普通生命周期，显式启动时传 `--es ble_lifecycle app`。

已有配对时，默认启动要求 `host_address` 为发送端实际已配对条目的地址，并核对目标；不要仅按名称自动选择设备。BLE 地址可能不同于 iPhone 的 USB 公开地址。有现存 shell 权限的 Android 16 可通过 [BluetoothDevice 身份地址接口](https://developer.android.com/reference/android/bluetooth/BluetoothDevice#getIdentityAddressWithType()) 只读核对两者。

完整服务交接可能受到 iPhone GATT 缓存影响。Android 16 及以上另提供显式“先托管、后配对”入口，需要 shell 原有的 `BLUETOOTH_PRIVILEGED` 权限，不自动授予权限：

1. 核对目标 iPhone 的公开蓝牙身份地址，填入上述 `host_address`；例如已授权 USB 连接上的 `ideviceinfo -k BluetoothAddress`。先结束输入会话，并仅由用户清除两端本次测试的旧配对。
2. 在安卓桥接前台启动托管实例及有限配对窗口：

   ```bash
   iatest mobile bluetooth-hid-owner-start connection.json --artifact-dir native/mobile/build/android-hid --lifetime-seconds 3600 --pairing-window-seconds 300
   ```

3. 确认 `registered` 为真、`pairing_state` 为 `waiting`，再从 iPhone「辅助触控 → 设备 → 蓝牙设备」选择安卓，由用户确认两端系统配对。BLE 不需要安卓的经典蓝牙限时发现。
4. `pairing_state: bound` 表示已配对设备身份匹配；仍需真实点击、输入和业务读回验收。后续界面重开继续使用同一托管实例，不重复配对。

配对窗口只允许前台心跳有效时读取配对所需的服务元数据；未配对或身份不匹配不能发送输入。窗口限定为 5–300 秒，未绑定目标就到期退出；绑定后关闭窗口，按原托管期限运行。重复启动不会延长窗口或期限。系统不支持身份核验、目标旧配对尚在、窗口过期或绑定其它设备时明确拒绝，不自动确认配对或删除配对。

托管模式及完整服务恢复的真机范围见 [完整服务恢复](#可选完整服务恢复)。默认不切换安卓蓝牙时的失败对照仍保留，不能将显式恢复策略的结果推广到默认模式。

## 可选完整服务恢复

普通 App 完整退出、托管进程退出或安卓蓝牙重启会使原服务消失，iPhone 可能保留失效的服务缓存。显式 `--restart-bluetooth` 策略先重启安卓蓝牙以真正断开链路，再重建框架服务，保留两端配对；会短暂断开这台安卓的其它蓝牙连接。默认启动及输入会话不自动切换蓝牙。

```bash
# 一次恢复：要求桥接没有活动或结果不确定的输入会话。
iatest mobile bluetooth-hid-recover connection.json --artifact-dir native/mobile/build/android-hid --restart-bluetooth

# 在当前终端持续监控，最多 7200 秒；不要关闭这个宿主进程。
iatest mobile bluetooth-hid-watch connection.json --artifact-dir native/mobile/build/android-hid --lifetime-seconds 3600 --restart-bluetooth

# 本地停止监控；手机离线时也可撤销后续自动恢复。
iatest mobile bluetooth-hid-watch-stop connection.json
```

连接配置可使用 `app` 或 `adb_managed` 生命周期，均须明确 `transport: ble`。切换到普通 App 前先显式停止现有托管服务；普通 App 使用已核对的实际配对地址。Android 16+ 托管重建可按特权接口返回的身份地址匹配，即使系统重启后仅缺失地址类型，也不会将正确身份误判为未配对；接口权限不足时仍需精确配对地址，不按名称猜测目标。

监控期限为 15–7200 秒。复用托管实例不延长它已有的期限；后续重建只使用宿主原期限内的剩余时间，最多恢复十次。`bluetooth-hid-owner-stop` 同时撤销对应监控，防止显式停止后再次启动。监控到期或结束会尝试停止自己最后持有的托管代次，不接管或停止外部更换的代次。普通 App 的服务仍遵循自身前台与进程生命周期。

监控等待授权 ADB、安卓启动完成、用户首次解锁和蓝牙开启，再恢复服务。用户保持蓝牙关闭时不会主动开启；监控自己请求重启蓝牙后，会在取消或异常路径尝试恢复开启。手机离线时可以取消宿主监控，但不能保证已经结束手机上仍存活的托管进程；该进程原有的有限期限仍有效。宿主退出或重启后不会自动重新授权监控。

托管登记包含实际内核启动 ID，系统重启后不会因旧 PID 被复用而等待错误进程退出；同一次系统启动内，无法证明已退出的进程仍禁止接管。普通 App 监控同时检查进程代次和蓝牙关闭/开启，覆盖 PID 不变但 GATT 已重建的场景。生命周期交接先选择目标所有者，再重启蓝牙，避免重连到交接前的服务表。

恢复使用设备租约，不打断另一个活动输入会话；新界面必须与新托管代次一致。恢复仅检查注册、连接和订阅，不发送测试文字，也不重放断连前的未知输入。`recovered: true` 与 `service_recovered` 都不代替实际业务读回；原输入调用失败后，由调用者结束旧会话并重新观察页面，再明确发起新操作。

## 查看状态与发送命令

```bash
iatest mobile bluetooth-hid-status ANDROID_SERIAL
iatest mobile bluetooth-hid-status ANDROID_SERIAL --list-hosts
```

后一个命令在本地显示已配对设备地址，操作者明确选择目标 iPhone；地址与 UDID 的对应关系需要人工确认，地址不是 App/业务结果证明。不要依据相同设备名称自动匹配另一台手机。

命令文件：

```json
{
  "connection": {
    "android_device_id": "ANDROID_SERIAL",
    "host_address": "AA:BB:CC:DD:EE:FF",
    "transport": "ble"
  },
  "commands": [
    {"op": "text", "text": "hello"}
  ]
}
```

```bash
iatest mobile bluetooth-hid-run commands.json
```

独立命令输出 `delivered_unverified`，只说明关联的发送回执已收到。键盘文本按 US 布局编码 ASCII；语言输入法、候选选择与最终文字需要另外观察和断言。协议包含相对移动、鼠标按钮、滚轮，以及需要实际校准与业务验收的绝对指针入口。

### 实验性 BLE digitizer

连接 JSON 可显式增加 `"experimental_digitizer": true`（仅 `transport: ble`）。默认关闭；改变此项会重建报告表，先结束输入会话，托管模式还需显式停止原所有者再创建。HOGP 报告表包含输入报告 4 和最大触点数特征报告 5，组合描述符限制在 512 字节；最多五个并行触点、ID 0–4，每条路径至多 64 帧、五秒，并必须以全部释放结束。USB 的独立报告格式不因此缩减。已有键盘、绝对指针、相对鼠标编号不变。

只有主机在当前连接实际写入触摸 CCCD 后，握手才公布 `digitizer` 和 `max_touch_points: 5`；不会从旧订阅缓存推断此能力。`touch` 命令沿用 USB HID 的 `frames: [{t_ms, contacts: [{id,x,y}]}]`，坐标范围 0–32767。Python、界面进程和托管进程均校验报告、身份及释放；投递不确定不重放，前台或心跳失效时尝试释放残余触点。能力表示协商后的报告端点，不表示目标系统已将报告映射为 App 触摸。

执行 ASCII 计划前在 iPhone 选定英语硬件键盘布局。中文拼音输入法可能把字母留在组词区，发送成功不代表已提交相同文字；控制器不会自动猜测或切换系统输入法。

带滚轮的 `move` 将总刻度拆为每 16ms 一个单位刻度，并在末尾发送零报告；X/Y 总位移和滚轮总刻度保持不变。iOS 会过滤、加速滚轮输入，实际距离需要 UI 后置断言，不能从报告数换算固定像素。时序对照见 [HID 滚轮实测与系统实现说明](https://ghosthand.dev/docs/scroll)。

## 接入正式 iOS 计划

在现有物理 iOS profile 中增加：

```json
{
  "ios_input_backend": "android_bluetooth_hid",
  "bluetooth_hid": {
    "android_device_id": "ANDROID_SERIAL",
    "host_address": "AA:BB:CC:DD:EE:FF",
    "transport": "ble"
  },
  "ios_observation": {
    "transport": "reverse_tls",
    "host": "192.168.1.10",
    "port": 0
  }
}
```

输入使用显式 `hid` 步骤，再接原生 `expect`/`wait`；该模式会拒绝隐式 XCTest 的 tap/text/swipe/gesture、输入片段及 WebView click/fill/press。控制台的 profile 高级 JSON 支持上述配置。

绝对定位的 `target_click` 还需要 `bluetooth_hid.pointer_calibration` 中的精确屏幕 `width`、`height`、方向 `rotation`。派发前重新验证原生目标唯一性、几何和方向；报告端点存在不证明手机执行了对应点击。仅针对自有 Lab 的真实验收脚本会读取当前屏幕几何，并以实际随机文字保存及重启读回来验收这份校准：

```bash
python scripts/validate_android_bluetooth_hid.py \
  --connection connection.json --ios-profile existing-ios-profile.json \
  --output /path/to/hid-result.json
```

中文拼音输入使用同一套只读候选与文字验证。先在手机选择中文拼音输入法，再执行：

```json
{"op":"hid","target":{"identifier":"nickname_field"},"hid_command":{"op":"ime_text","focus":"target_click","candidate_commit":"space","text":"中文","pinyin":"zhongwen","expected_value":"中文"}}
```

`focus: target_click` 明确使用 HID 点击编辑框，适用于原生观察没有键盘焦点属性的情况。`candidate_commit: space` 先观察完全匹配的唯一候选，再用 HID 空格提交当前选中的候选，最后完整读回编辑框；要求三个动作及至少六次查询。预期候选没有被选中或最终文字不同都会失败。这条路径适用于已实测的外接键盘浮动候选条。

省略 `candidate_commit` 时仍以指针点击候选，要求三个动作及至少七次查询，并保留“候选属于当前软件键盘”的原生检查；外接键盘浮动候选可能不在该软件键盘子树中。省略 `pinyin` 时才按需加载已获授权的 `iatest-framework[hid]` 可选依赖。没有候选、候选歧义、值被截断或只有同名占位提示均不能证明输入通过。

也可显式使用 `"candidate_commit":"target_click"` 并提供 `"candidate_target":{"type":"cell","identifier":"中文"}`。该模式查询调用方明确指定的唯一候选控件，要求其文字或 identifier 与目标中文完全匹配，重新检查几何后用 HID 点击，并读回编辑框完整值。返回 `candidate_scope: explicit_target`，不提供自动的系统键盘归属证明；默认 `pointer` 模式的键盘检查保留。详细朗读 label 不必与中文完全相同，但须有完全匹配的 identifier。

在手机预先选定中文拼音键盘后，可用上述验收命令增加 `--pinyin`，执行两轮“中文＋随机数字”的保存、截图和重启读回；默认模式继续验证英语 ASCII。

使用 `--pinyin --candidate-commit target_click` 可验证自有 Lab 的上述明确候选控件模式。桥接 App 现在保存最近选择的传输模式；首次默认仍为经典模式，选定 BLE 后从桌面重开和冷启动均保留 BLE。普通桌面 Intent 不切换模式，活动会话期间的模式切换仍拒绝。

若需要主动请求重连，可运行 `iatest mobile bluetooth-hid-reconnect connection.json`，先建立精确主机绑定的控制会话、确认释放，再建立并释放本 App 的连接引用、恢复广播。其它系统 GATT 注册可能继续持有连接，该操作不保证实际物理断连，也不保证 iPhone 缓存恢复。它不切换全局蓝牙或删除配对；未知输入会话会被拒绝，`reconnect_requested` 不代表输入已恢复。不得重放断连前的未知输入；失败恢复的实际对照见[恢复记录](bluetooth-hid.md)。



## 输入与恢复边界

普通 App 生命周期、ADB 托管生命周期和宿主有限监控是显式选择。完整服务重建时的蓝牙缓存不能靠“已连接”判断；需要恢复策略时明确启用 --restart-bluetooth，它会短暂中断安卓其它蓝牙连接。默认不切换蓝牙，不重放断连前的未知输入。

BLE digitizer 仅为默认关闭的实验报告通道；当前 iPhone 没有形成真实多指触摸业务结果。连接、订阅、发送成功与业务通过分开判断。中文候选以最终文本读回为准，有限恢复不替代设备解锁、前台和权限要求。
