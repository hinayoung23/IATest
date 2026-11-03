# 移动执行与跨控制端资源

## 连续视频

检查器可在 PNG 与 H.264 间选择，短时预览才开启媒体。Android 使用 UiAutomation 截图、EGL 与 MediaCodec，iOS 使用截图和 VideoToolbox。profile 可显式选择 Android display mirror 到编码 surface，或开发版 iOS App 的 ReplayKit CVPixelBuffer 流；后者需系统录屏同意。编码最大边长 720、目标 15 FPS、2 Mbps，默认每条流限制 120 秒/64 MiB。实际速度由设备决定，不据此推断达到 scrcpy 的效率。

需要长时间采集时，在设备 profile 显式设置预算，例如：

```json
{"media_limits":{"duration_seconds":3660,"byte_limit":1073741824}}
```

时长必须为 1–7200 秒的整数，累计数据预算为 1–2147483648 字节。宿主与新版 Android/iOS 原生驱动均执行预算，握手必须确认同一组值；旧驱动未确认时拒绝启动，不自动重建媒体来隐藏耗尽。需重建相应原生驱动；独立 ReplayKit App stream 不接受该扩展。预算不是保存整段视频的承诺，接收器仍只保留有界数据。

运行本身的超时预算也必须覆盖采集时间；增大媒体预算不会延长计划或 `RunControl` 的期限。显式预算耗尽后，需要先停止该预览，再明确开启新流。

源码中的 `scripts/validate_android_video_soak.py --project PROJECT --profile PROFILE --seconds 3600 --output NEW_RESULT.json` 在自有物理 Android Lab 上检查同一接收器和编码代次、定期真实 FFmpeg 解码、原生点击读回、进程 PID/内存及清理。测试不会把分段会话或静止画面的空闲回执计成新增视频帧。

宿主使用已有 FFmpeg 解码，不捆绑其二进制或新增 Python 依赖；不可用时明确报错。编码数据保持预测依赖，解码后只保存最新一帧；尺寸/画面代次变化、积压或解码失败会重建解码器并请求关键帧，恢复最多三次。停止预览、隐藏页面或关闭会话时释放通道和所属进程。视频不会逐帧发送给模型，也不能作为点击的坐标来源。

```bash
iatest mobile preview PROFILE output.png --codec h264 --project demo
```

该命令保存一个解码帧并关闭会话，元数据保留来源和帧年龄。浏览器可选择 H.264 WebCodecs 直接解码至 Canvas，无宿主 PNG 解码中转；FFmpeg/PNG 模式仍可显式选择。Android 真机 30 秒 mirror、重连关键帧和宿主解码通过，静止画面允许明确的 `idle=true` 零负载回执；浏览器保留原画面并显示暂无新帧，宿主模式标明 `source_idle` 和 `last_frame_age_ms`，不把旧帧记为新增帧，见[续做验收](mobile-testing.md)。iOS 已取得真实 PNG、H.264 编码及宿主解码首帧，实际 Chromium WebCodecs 解码到 Canvas 像素也通过，不据此宣称持续性能。

机制依据：[Android MediaCodec](https://developer.android.com/reference/android/media/MediaCodec)、[Apple VideoToolbox](https://developer.apple.com/documentation/videotoolbox)、[FFmpeg image2pipe](https://ffmpeg.org/ffmpeg-formats.html#image2pipe)。

## 持久局部节点缓存

每个驱动最多 16 个区域、每区最多八个节点。Android 持有有界 AccessibilityNodeInfo，刷新节点、检查窗口/容器，按相关区域事件失效，TTL 500ms；iOS 持有局部 XCUIElement，按 App 几何/旋转、动作及 250ms TTL 失效。各自另有最多 32 个编译配方。

缓存只用于明确允许非唯一候选的探索查询。命中结果 complete=false、match_count_is_lower_bound=true，不能证明唯一或不存在。动作、断言、稳定回放和视觉截图均重新查询；节点过期/窗口变化/缓存关闭时释放，不能以“缓存命中”替代当前观察。

检查器有允许缓存候选和清空缓存入口。CLI 可比较多次查询和关闭缓存：

```bash
iatest mobile query PROFILE target.json --repeat 3 --project demo
iatest mobile query PROFILE target.json --repeat 3 --fresh --project demo
```

target.json 中设置 require_unique=false 才可获得缓存候选，结果保留命中、失效与数量信息。缓存不提供性能收益保证。

Android 新驱动在页面事件频繁、查询不完整时，最多进行 3 次只读快照尝试，共享 250ms 和已知节点访问预算；后续 ID 查询可改走有界遍历。每次仍核对状态变化，不把不完整结果或缓存命中提升为唯一/不存在证明，不重试输入。`query_attempts` 与会话累计指标记录尝试次数，平台内部访问量仍标为未知。

## 移动视觉定位

Android 可先使用 `iatest mobile outline PROFILE --project demo` 获取少量控件及区域；控制台“页面控件概览”和目标探索共享原生入口。默认最多 512 节点、12 个候选和 250ms，结果不是唯一性证明。无名称控件可显式使用 `type`、空 `label` 和 `bounds` 组合过滤。`bounds: [left, top, right, bottom]` 表示当前控件矩形的精确匹配，Android 使用整数像素，iOS 使用可含小数的屏幕点。iOS 需重建 Runner（能力 `spatial_bounds`），每次最多扫描 512 个语义候选，受查询期限限制；建议组合类型或 identifier 缩小范围。先等待布局稳定，后续输入仍重新核对矩形、唯一性及完整性；超时或超预算不能证明不存在，不使用历史节点缓存证明空间定位。

Android `tap` 默认采用坐标触摸。可显式设置 `tap_backend: accessibility` 调用节点 ACTION_CLICK，但驱动和当前唯一目标必须声明支持；控制台人工动作有同一选项。没有自动回退，也不把辅助功能点击当成真实触摸链路测试；两种模式都要验证业务后置条件。

```bash
iatest mobile visual-find PROFILE target.json '定位区域中的保存图标' --project demo
```

区域过大时可加 `--visual-crop '{"x":0.4,"y":0.6,"width":0.2,"height":0.1}'`，坐标归一化到当前原生目标，不是全屏点击坐标。控制台提供 ROI JSON 和高级 target JSON；探索工具支持同一参数。固化 `visual_click` 保存 `visual_crop`，范围必须完全位于目标内部。透明图标的背景变化仍可能导致像素校验拒绝，ROI 不保证动态区域稳定。

生成模型须明确配置 supports_images=true。手机先裁剪指定的唯一原生目标，最多 1024 边长/524288 像素/1 MiB，再经统一审计入口送一张图片、最多调用一次模型；兼容工具调用及通过同一 Schema 校验的纯 JSON 候选。坐标 x/y 是左上角，width/height 是宽高，最多三个候选，仅唯一候选允许探索点击。

检查器有局部视觉定位与手动点击入口；目标探索同样支持 Android/iOS 视觉候选。固化计划使用 visual_click、target 和 visual_anchor，最后仍须确定性 expect。点击前重拍，核对像素、屏幕尺寸/比例/旋转、会话、目标边界与时效，再由原生输入派发。图像改变即拒绝，不使用旧预览或跨运行句柄。iOS 需重建含 visual_timestamp_checks 的 Runner；截图请求至输入前采用固定 2 秒预算，Runner 用单调时钟检查最新截图，并在原生目标复查前后核对时效；避免宿主额外一次查询耗尽预算。当前 rotation 来自 UIDeviceOrientation，不能当作 UI 旋转角度，本次仅验竖屏。图片模型输出是建议，真实业务断言决定结果。

## 独立多触点轨迹

gesture 使用唯一 target 与 contacts，每个触点有独立 id、points，每点为 t_ms/x/y；坐标归一化到当前目标区域。最多 10 触点、256 点、5 秒，单触点时间严格递增且至少两点，第一个触点从 t=0 开始。参见 [多触点示例](../../examples/plans/android-multitouch.json)。

Android 按时序插值，派发 POINTER_DOWN/MOVE/POINTER_UP，并在异常时取消仍持有的触点。iOS 扩展轨迹通过软链接 XCTest 的事件记录接口，需 profile 中 allow_private_ios_gestures=true；控制台设备高级设置有开关。启动时逐项探测选择器，不兼容就拒绝执行。iOS 每条记录包含最终 lift；未知完成或超时不重放，平台内部执行无法硬抢占，不能保证中断瞬间全部触点已释放。

```bash
iatest mobile gesture-preview examples/plans/android-multitouch.json
```

此命令仅在宿主预览时序。实际执行继续使用 mobile import-plan/run 或控制台 JSON 编辑器。普通双指缩放仍可直接使用 pinch。

iOS 内部接口签名结合本机 SDK 符号及 [WebDriverAgent 接口调用](https://github.com/appium/WebDriverAgent/blob/master/WebDriverAgentLib/Utilities/FBXCTestDaemonsProxy.m)核对；未捆绑或复制其执行后端，兼容性需随 Xcode 维护。

## 显式 IME

ime 包含 target、ime_backend、ime_events，最多 24 个事件、40000 字节，最终事件必须 expect，计划最后另有业务 UI 断言。事件为 compose/candidate/commit/cancel/expect，完整序列先预检动作预算。参见 [Android 示例](../../examples/plans/android-ime.json)。

- Android input_connection：自有测试输入法使用真实 InputConnection 的组合、提交、取消接口，候选来自明确输入的测试列表，不代表三方输入法的词库或预测算法。可验证 composing、before_cursor 和系统回调的 marked 状态，不返回原始输入值。未提交的组合在序列结束时尝试取消，清理不能确认时保留未知结果。
- Android profile.allow_ime_setup=true 才允许会话内选择测试输入法；默认关闭。先保存默认/启用状态，结束后恢复自身变更，外部已切换的默认输入法不覆盖。无法确认恢复时报告清理问题。输入法服务仅允许系统通过 BIND_INPUT_METHOD 绑定。
- iOS keyboard_ui：使用实际当前键盘输入、精确可见候选、明确的提交/取消键。expect 使用原生字段 value，不能把它称为 UITextInput 的 marked range。组合字符依赖当前键盘布局；取消尝试恢复输入前的字段值，未恢复则未知。无法观察的 marked/composing/before_cursor 条件在准备阶段拒绝，不补造通过。

改变输入法、输入权限、具体键盘布局及标记区兼容性仍需设备验收；语义 setText 保持独立模式。iOS 开发构建新增只读 input_state_expect，须绑定已验证构建、原生目标和前序动作的新鲜证据；替代 Simulator 输入、剪贴板、跨 App/WebView 和多显示屏见[扩展覆盖说明](../reference/capabilities.md)。

## 跨独立控制端资源

共享资源服务维护独立数据库。身份文件仅包含既有 token_env 引用和资源允许列表，例如：

```json
[{"id":"controller-a","token_env":"IATEST_RESOURCE_TOKEN_A","resources":["lab:shared"]},
 {"id":"controller-b","token_env":"IATEST_RESOURCE_TOKEN_B","resources":["lab:shared"]}]
```

```bash
iatest node coordinator identities.json authority.sqlite3 --port 8769
iatest node coordinator-config authority-client.json --project demo
iatest node global-claims --project demo
iatest node global-reconcile LEASE --stopped --note '已核对旧控制端、工作进程和设备输入停止，以及业务副作用' --project demo
```

客户端配置：

```json
{"url":"http://127.0.0.1:8769","id":"controller-a","token_env":"IATEST_RESOURCE_TOKEN_A","ttl":10,"wait_seconds":0}
```

各独立控制端配置相同服务地址及自己的身份，局域网必须 TLS/可信 CA。CLI/Web 可启动/停止所属服务、配置客户端、查看自己的占用并对账。单 SQLite 服务不承诺高可用；需要多副本时可配置真实 etcd 共识存储、固定 authority_id 与 urls 列表，见[扩展覆盖说明](../reference/capabilities.md)。未配置的控制端与框架外工具不自动受到约束。

计划顶层 resources 声明全局资源；移动 profile 自动推导 device:平台:设备ID，远程节点登记资源和版本任务资源也纳入协调。显式全局资源缺服务或可信父任务授权时阻断。批量取得、幂等取得、TTL 续租和单调 fencing 由服务负责；线程/隔离进程/远程节点沿用父任务授权，不重复抢同一资源。

租约丢失会停止后续调度与输入；已经派发的原生操作仍遵循其平台中断边界。超时占用保留 uncertain，不自动转交。对账必须由原身份明确确认旧进程、设备输入及副作用，留下审计后释放；旧代次不能续租。没有远程硬件证明，不保证能阻止恶意主机或已脱离框架的输入程序。

## 独立 Android VM

配置示例 [独立 VM 示例](../../examples/devices/android-emulator.json)。先通过既有 App 来源入口注册已核验的 APK，`managed_emulator.artifact_id` 引用该资产，SDK、emulator、platform-tools 和选定系统镜像需预装。CLI `mobile import-profile` 和控制台 Advanced profile JSON 均可保存；原移动计划继续引用这个 profile。

每次会话新建私有 AVD、控制台/ADB 端口租约和可写数据盘，不使用现有 AVD、共享快照或已有物理机数据。控制器确认所属进程、AVD 名、qemu 标识和启动就绪，再安装已核验 App 并启动探针。`require_os_sandbox: true` 仅允许当前控制器实际持有的 VM，不能给物理手机加一个声明就通过。结束、超时或启动失败会终止所属子进程、回收端口及临时数据盘；重开是空数据环境。

边界是独立 Android 内核/数据盘的 VM，网络仍为模拟器 NAT；它不自动成为网络出口白名单或高强度对抗环境。已有 root UID 网络规则仍是单独能力，不触碰现有手机。Android 模拟器使用软件 GPU，不用于真实手机 GPU/能耗精度验收。宿主强制崩溃可遗留实例/临时目录，需要按实际所属进程处理；不按名称批量杀进程。


## 授权 root UID 出口限制

profile 可显式配置 `isolation.backend: android_root_firewall` 和 `allowed_endpoints: [{ip, port}]`。要求授权 root、目标为非系统且不共享 UID。独立 IPv4/IPv6 链只允许明确 IP/TCP 端口，失败回滚，结束清理所属规则。不控制代理服务代发流量或所有平台 IPC，宿主崩溃后需核对遗留链。它不等于整机 OS 沙箱。
