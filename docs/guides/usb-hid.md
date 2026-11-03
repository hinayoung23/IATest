# 外接 USB HID 键盘与鼠标

也可以复用现有安卓手机作为蓝牙发送端，免去 USB 桥接板；配置、已验收范围及仍在验证的定位能力见 [安卓蓝牙 HID](bluetooth-hid.md)。下文描述的是 USB gadget 通道。

“发送端”指连接到手机、向手机发送 USB 键鼠报告的外部桥接设备。链路为 `Mac 上的 IATest → 独立控制连接 → HID 桥接设备 → USB → iPhone`。桥接设备要能以 USB 外设身份工作；普通数据线、Mac 或 Windows 的常规 USB 主机端口不能直接充当这里的 gadget。手机收到的是键鼠输入；截图和结果读回仍通过独立观察通道完成。

此通道输入不调用 XCTest。需要具有 USB Device Controller 的外部 Linux 桥接硬件（如支持 USB gadget 的设备）、与 iPhone 兼容的数据线/供电转接，以及独立的桥接控制连接。Mac 的普通 USB 端口不能直接代替 USB gadget。默认提供 US 键盘和相对鼠标，可另启用绝对指针和十触点 digitizer 报告端点。当前没有硬件验收记录，报告格式完成不证明 iPhone 接受外接触摸。

HID占用iPhone的USB口时，业务断言使用独立的反向TLS观察通道。Runner通过局域网主动连接Mac，控制消息与H264媒体分别绑定会话、App和通道角色；连接前固定核验本次会话的证书SHA256，手机令牌仅在加密连接内发送。不依赖手机USB转发、不改变系统证书信任或全局网络配置。实际键鼠/触摸效果仍需对应桥接硬件与目标系统验证，见[能力边界](../reference/capabilities.md)。

在完整iOS profile中显式增加：

```json
{
  "ios_input_backend": "usb_hid",
  "ios_observation": {"transport": "reverse_tls", "host": "192.168.1.10", "port": 0}
}
```

`host`填写Mac在同一可信局域网中的实际私有IPv4地址；`port: 0`使用临时端口。需事先在Xcode完成iPhone无线配对，确认断开USB后仍可启动签名Runner，并允许Runner访问本地网络。每次运行仍需签名的`xctestrun`和原有`usb_hid`桥接配置；上面仅为新增字段片段。物理机拒绝回环地址，Simulator可显式使用`127.0.0.1`验证传输。该通道使用Runner截图/编码，不支持与`video_capture: app_stream`组合；配置时明确拒绝，不自动回退USB。证书与私钥只存放在会话自有临时目录，结束时清理。

首次运行会激活自有Runner，使iOS能够显示“本地网络”系统授权提示；请由用户选择允许。TCP认证前的连接等待有时限，未授权或仍不可达会明确失败；不会重复业务输入。连接完成后激活已运行的目标App，不隐式启动已停止的App。`validate_ios_device.py --observation-host ... --require-wireless`可核对拔线前提与真实媒体验收。

## 桥接端

Linux 需已启用 configfs/libcomposite/USB HID function。脚本只创建新的 `iatest_hid`，不接管已有 gadget，也不改变内核启动或网络配置。硬件操作者提供合法的 VID/PID、UDC 和序列号。先在外部桥接机检查 dry run：

```sh
python scripts/configure_usb_hid_gadget.py --udc YOUR_UDC --vendor-id YOUR_VID --product-id YOUR_PID --serial YOUR_SERIAL
```

确认这些硬件参数后在桥接机显式加 `--apply`（需 root）。它配置8字节键盘和4字节相对鼠标/滚轮报告；核对实际 `/dev/hidgX` 分配，不假定系统一定分配0和1。失败时保留所属部分配置供检查，不删除或覆盖既有状态。移除时先停止服务、解除这个 gadget 的 UDC 绑定，再按 [Linux configfs 文档](https://docs.kernel.org/usb/gadget_configfs.html) 删除所属链接、function、配置和字符串目录；不要删除其它 gadget。

安装框架后运行服务，令牌通过你已有的环境变量提供，长度32至256个 ASCII 字母/数字，两端相同，勿写进 JSON、命令行或版本库：

```sh
python -m iatest.mobile.usb_hid_gateway --keyboard /dev/hidg0 --mouse /dev/hidg1 --binding-id owned-iphone-cable --token-env IATEST_USB_HID_TOKEN --port 19430
```

服务只监听127.0.0.1，用受控 SSH 转发连接到 Mac：

```sh
ssh -N -L 19430:127.0.0.1:19430 your-bridge-host
```

`binding_id` 是操作者确认的物理线缆标签，桥接机无法从 HID 自动证明手机 UDID。接线时在受控 Lab 确认目标，切换手机前关闭会话并更换绑定；不能将标签或回执视作 UDID/业务结果的自动证明。基础 `text` 按 US 键盘布局编码 ASCII；中文组合入口另见下文。鼠标需按 [Apple 指针设备说明](https://support.apple.com/en-au/111775) 设置 AssistiveTouch，本工具不自动改变手机设置。

## 主机入口

`examples/hid/usb-commands.json` 是独立入口：

```sh
iatest mobile hid-run examples/hid/usb-commands.json
```

支持 `text`（最多256个 ASCII 字符、换行/Tab）、`key`（USB HID usage及 modifier bitmask）、`move`（每轴/滚轮-127至127）和 `click`（左/右/中）。每个键/按钮均带释放报告；一次命令最多10秒，控制器/硬件各有独占租约与递增序列、会话代次和操作账本。断连、部分写入或回执失配视为未知，不自动重发。服务正常关闭及命令异常会尝试释放键/按钮；USB 丢失、强制杀进程及硬件断电时仍需实机验证恢复。

Standalone 输出 `delivered_unverified`，表示桥接写报告已返回；不代表 iPhone 已接收或业务通过。它不会启动 Runner 或查询手机。若需要业务断言，使用 `examples/devices/ios-usb-hid.json` 及普通 iOS UI 计划：

```json
{"op":"hid","hid_command":{"op":"text","text":"hello"}}
```

后面接原生 `expect/wait` 断言；这个组合仍需要 XCTest 观察通道。`ios_input_backend: usb_hid` 会拒绝 tap/text/swipe/gesture/fragment 等隐式 XCTest 输入，WebView 的 click/fill/press 亦拒绝。

## 绝对指针、中文组合及触摸报告扩展

配置 gadget 时显式增加 `--absolute-mouse`、`--digitizer`；gateway 对应传入这两个参数及各自独立的字符设备路径。配置脚本默认只展示 dry run，仍需硬件操作者核对端点再显式 `--apply`。gateway 只公布已打开端点；digitizer 还必须成功注册 Linux `GADGET_HID_WRITE_GET_REPORT` 的最大触点 Feature Report，否则启动拒绝该扩展。旧内核可能不提供此接口，不能仅据 `/dev/hidgX` 存在判断触摸已可用。ABI 依据 [Linux g_hid.h](https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/usb/g_hid.h)。

独立入口增加 `point`，坐标范围0–32767，移动后按下并释放指定鼠标按钮；`touch` 使用最多64帧、5秒、10个唯一 contact ID 的显式时间表，首帧时间为0，末帧释放全部触点。抬起保留最后坐标，异常和服务退出尝试释放。它们只在桥接端协商了相应能力时可派发；独立入口结果仍是 `delivered_unverified`。

iOS UI 计划另提供 `target_click` 和 `ime_text`。profile 的 `usb_hid.pointer_calibration` 须由硬件实测设置 `width`、`height`（XCTest屏幕坐标单位）和 `rotation`（Runner 报告的方向枚举，竖屏为1）。使用新构建的 Runner，只读核对唯一目标、可交互区域、屏幕几何和方向；几何变化或观察超时会阻断派发。目标点击示例：

```json
{"op":"hid","target":{"identifier":"save"},"hid_command":{"op":"target_click"}}
```

中文组合须先明确聚焦非安全编辑框，并在设备上选定支持拼音的输入法。安装已授权的可选依赖 `pip install 'iatest-framework[hid]'`，或在命令中显式给出 `pinyin`：

```json
{"op":"hid","target":{"identifier":"name"},"hid_command":{"op":"ime_text","text":"你好","pinyin":"nihao","expected_value":"你好"}}
```

控制器先输入拼音，再从当前键盘中观察到与中文完全匹配的唯一候选，重新验证几何后用 HID 指针选择，最后读回编辑框的完整值。无法观测候选、焦点/方向变化、歧义或最终值不符均失败，不因拼音已发出判通过，也不会重打未知输入。可用 `candidate_target` 缩小原生候选定位范围；不能指向键盘以外的同名元素。当前只覆盖有界拼音组合，不承诺任意 Unicode、所有输入法或多音字自动消歧。

如果原生观察通道不提供编辑框的键盘焦点属性，可显式增加 `"focus":"target_click"`：先用 HID 点击经过几何校验的编辑框，重新查询目标，再输入拼音。该模式计入三个输入动作和至少七次查询，仍必须验证当前键盘候选及最终完整文字；默认的 `require_focused` 不自动切换模式。

外接键盘使用浮动候选条时，可显式增加 `"candidate_commit":"space"`：观察完全匹配的唯一候选后，用 HID 空格提交当前选中的候选并读回完整值；如果选中的词不同则失败。该方式不点击候选元素，不需要候选指针几何检查；与 `focus: target_click` 组合时需要至少六次查询。默认指针模式仍保留当前软件键盘的归属检查。

`candidate_commit: target_click` 则要求显式 `candidate_target.identifier`，以经过完整唯一查询和几何复核的目标控件提交；候选文字或 identifier 必须等于目标中文，最后完整读回编辑框。其证据范围为 `explicit_target`，与默认模式的系统键盘归属检查分别记录。该共享输入逻辑已用安卓 BLE 到 iPhone 验证，USB 硬件路径仍需独立验收。

协议、描述符位布局、GET_REPORT ABI、指针/触摸字节、拼音转换、候选边界、部分失败、释放、租约、鉴权和无重放有本地测试；其中使用内存报告接收器或本地协议服务，不属于硬件输入验收。待硬件接入后仍需在自有 Lab 验证实际文字、点击、布局/校准、主机触摸识别、连接恢复和释放，并由独立 UI 断言证明结果。普通未越狱 iPhone 的任意全局多指触摸尚无支持证明。报告格式依据 [Linux HID gadget 文档](https://docs.kernel.org/usb/gadget_hid.html)，支持范围见[能力边界](../reference/capabilities.md)。
