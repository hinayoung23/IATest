# iOS 性能采集与离线门禁

使用已有 Xcode、签名 Runner 与授权设备，显式选择采集方式。Apple 原始附加、收尾及零帧字段问题仍保留，不以兼容路径伪称已修复。

## 独立运行方式

- 实时 UI 测量：在 iOS UI 计划外包装 `client_performance`，显式配置 ios_capture.mode=attach/launch/device_processes。默认 activity_monitor 采集进程资源；frame_lifetimes 精简采集独立帧区间，animation_hitches 保留完整模板。
- 历史测量评估：导入已有 `.trace`、xctrace XML、明确绑定的 xcresult 或类型化测量 JSON，生成不可覆盖的测量资产；另建离线计划执行门禁。此模式不连接手机、不安装 App、不执行 UI，不把历史测量当成本次运行的指标。
- 原生 XCTest 测量：`mobile ios-measure` 读取规格文件和签名 profile，执行启动或受控滚动，精确提取 xcresult；`mobile ios-calibrate` 仅在自有 Debug Lab 上执行正负对照，保存 usable/rejected 校准资产。

在控制台“多类型测试 → 客户端性能”中可检查宿主工具链、导入测量、填写自己的门禁阈值、创建计划并执行，也可对结果发起 AI 证据分析。

## 工具检查与实时录制

```bash
iatest mobile perf-doctor
```

此命令仅检查宿主 `xcrun/xctrace` 和模板，不枚举或操作手机。配置示例见 `examples/plans/ios-live-performance.json`：

```json
{
  "id": "ios-profile-journey", "kind": "client_performance",
  "budget": {"timeout_seconds": 300},
  "spec": {
    "platform": "ios", "iterations": 2, "warmup": 1,
    "ios_capture": {"target_pid": 777, "duration_seconds": 60, "ready_timeout_seconds": 20, "max_trace_bytes": 268435456},
    "plan": {"id": "check-ready", "kind": "ios", "spec": {"profile": "ios-local", "steps": [{"op": "expect", "target": {"text": "Ready"}, "attribute": "exists", "expected": true}]}},
    "thresholds": {"max_cpu_percent": 80, "max_memory_footprint_kb": 200000}
  }
}
```

`777` 是占位 PID，指定时必须替换；也可省略 target_pid，让框架按 devicectl 的 App 标识/安装路径解析唯一正在运行的进程，无法确认或存在多个候选时停止。附加录制不负责启动 App，旅程内禁止 launch/terminate，结束后再次核对该 PID。App 应事先安装、运行并处于适合执行的状态；旧 PID 不会被当作新安装进程自动使用。

显式 `mode=launch` 不接受 target_pid，会停止已有目标 App、由 Instruments 启动并绑定实际 PID；会话先准备 Runner，再录制、激活目标并核对 PID，结束时只清理本次拥有的进程。数据沙盒保留，不清除数据。已有不同进程不会被当作本次进程终止。示例为 `examples/plans/ios-launch-performance.json`；这不是启动耗时测量，启动耗时使用独立 XCTest 方法。

launch 在 UI 结束、录制收尾前核验所属 PID；Instruments 收尾后该所属进程可能已退出，按实际状态确认清理，仍要求 trace 绑定原 PID 和正确设备。非所属的 attach/device_processes 模式继续要求录制后原 PID 存活。该生命周期规则不证明 App 业务健康或持续存活；UI 预期仍由计划断言判断。

显式 `mode=device_processes` 要求目标已运行，可自动按 App 身份解析 PID。只用 Activity Monitor 临时录制设备进程资源统计，再严格按目标 PID 提取；记录前后、激活前后均核验 PID，变化即失败。不会主动重启或清理目标 App，不在 attach 失败后自动选择。来源保留真实 TOC target_pid=0，并另记 selected_pid/recording_scope；临时原始设备统计在结束时删除。示例为 `examples/plans/ios-existing-performance.json`。

显式 `template=animation_hitches` 导出 hitches-updates 和 hitches-frame-lifetimes，按准确 PID、display、swap-id、surface-id 关联并报告 frame_lifetime_ms。关联不完整或存在歧义则不能通过门禁。区间覆盖整个录制窗口，含启动/激活开销，不代表单步精确计时、纯渲染时长或由刷新率推测的帧预算。示例 `examples/plans/ios-frame-performance.json`；Web 客户端性能页提供以上三个示例入口。

显式 `template=frame_lifetimes` 使用当前 Xcode 自带的空白模板，仅添加 Hitches 和 Frame Lifetimes 仪器，继续使用上述两张原始帧表及同一质量门禁。它不包含 CPU/内存或完整诊断模板的其它观察。框架不修改或分发 Apple 模板；来源保存实际 TOC 模板名 Blank、capture_template、仪器名、模板 SHA256 和 trace_bytes。模板不可用、Xcode 准备过程中变化或未导出帧表时拒绝。Web 帧区间示例使用该模式；完整模板仍可显式选择。

已有 App 不重启的完整示例为 `examples/plans/ios-minimal-frame-performance.json`：

```json
{"mode":"attach","tool_launch":"isolated_copy","template":"frame_lifetimes","stop_strategy":"time_limit","duration_seconds":15,"max_trace_bytes":268435456,"finalize_timeout_seconds":45}
```

完整采集模板可能产生较大数据。容量检查不是 OS 磁盘配额，保存阶段可能短时超限；超限保留 trace_budget_exceeded/trace_validation，不自动放大额度。

宿主等待 CLI 明确就绪后才执行 UI，随后中断录制进程完成保存。录制时长、就绪时限、输出/导出大小、样本量及清理时间均有上限；`finalize_timeout_seconds` 可设 1–60 秒。过早结束、导出失败或缺测不能通过。录制窗口含开始/结束开销，指标为窗口内观测最大值，不宣称绝对峰值。白名单错误携带 code/phase，原始 stderr、环境和 trace 不进入模型。

导出使用不超过任务剩余时间的 20 秒子预算，继承取消并在返回后复核；取消/总时限不会被正常测量完成覆盖。收尾诊断通过父结果的 capture_diagnostics/capture_failures 保留 code、phase 和 iteration；原 UI 断言与测量质量分别记录。进程在信号发送前退出仍会回收并清理临时文件。

Activity Monitor 支持进程 CPU、physical footprint、resident memory，不推测启动/帧指标。自有 Runner 新增 XCTest 的 ApplicationLaunchMetric 和 scrollingAndDecelerationMetric 测量入口；后者实际提供哪些指标须由 xcresult 精确绑定确认，不能把 FPS 换名为帧耗时，也不把 CPU 时间当 CPU 百分比。规范 JSON 导入与实际 XCTest 执行分别记录。

执行规格包含 profile、mode（launch/scroll）、configuration_id、bindings 和可选 iterations（1–10）；scroll 另需 target_identifier。bindings 明确 ios_launch_ms/frame_duration_ms/hitch_ratio 的 identifier/unit，仅接受 s/ms/us/ns 或 ms/s、ms per s、ratio 换算。默认 test_id 为 MobileRunner/testMeasureLaunch 或 MobileRunner/testMeasureScroll，以实际导出为准。指标重复、单位或样本对齐变化直接拒绝；缺失/零帧保留原始卡顿样本并标记质量未评估，不能自动通过。框架不猜 displayName，不由 FPS 换算帧时长。

### 签名工具副本兼容启动

原始 attach 出现 `target_not_found` 时，可显式设置以下 `ios_capture` 对象：

```json
{"mode":"attach","tool_launch":"isolated_copy","template":"animation_hitches","stop_strategy":"time_limit","duration_seconds":30,"finalize_timeout_seconds":45}
```

完整例子见 `examples/plans/ios-isolated-performance.json`。CPU/内存使用 `template=activity_monitor` 及对应阈值。Web 客户端性能页提供“iOS 单进程附加兼容采集”表单。

框架解析当前选定 Xcode 的 xctrace，复制到私有临时目录（最多64MiB），复制前后校验 SHA256 并验证原始签名；不修改加载指令、不重新签名、不分发 Apple 二进制。只为本次进程设置 Xcode 框架搜索路径，移除继承的 DYLD 环境项。正常结束、失败和取消均清理副本。

此策略保持真正 `--attach PID` 的进程范围，App 不被重启或终止；来源保留实际 TOC PID、`tool_launch/tool_sha256/tool_copy_byte_identical/tool_signature_verified`。`tool_launch=standard` 是默认值，无隐式重试或回退。该模式不修写 Apple 的零帧字段。

显式 stop_strategy=time_limit 会在UI完成后等待固定录制窗口自然结束；窗口包含余下空闲时间，仍受总体取消、容量和结束期限约束。UI结束前录制已经终止仍拒绝；结束后再次确认原PID。默认interrupt保持收到UI结束后发SIGINT。固定窗口不保证解除当前DTX远端结束等待，两种失败均不能产生通过门禁。

兼容attach在录制前激活已核验的原PID；激活前后都核对身份，变化或回执未知则停止。iOS Runner收到关闭确认后，宿主最多等待5秒让XCTest自然收尾，超时再结束自有进程；这改善清理，不证明Apple录制故障已修复。

XCTest 导入另保留 `frame_count_status`、`frame_rates_fps`、`zero_frame_count_with_nonzero_rate`，只接受同指标族、单位 fps、严格对齐的原始伴随样本。非零帧率不建立帧覆盖，也不用于补造帧数。

## 受控滚动与采集器校准

```bash
iatest mobile build ios --device --team YOUR_TEAM --performance
iatest mobile ios-calibrate examples/measurements/ios-scroll-calibration.json --project demo
iatest mobile calibrations --project demo
iatest mobile ios-measure your-scroll-spec.json --project demo
```

`--performance` 使用 Release Runner，关闭调试器和覆盖率；被测 App 的构建类型保持独立。profile 的 xctestrun 必须指向新 Runner。校准需要安装本仓库 Debug Lab，示例中的 profile/configuration_id 必须替换。业务 App 不允许自动注入。

滚动要求原生 table/collection/scroll 容器。可指定 `origin_identifier`；省略时采用唯一可见首 cell。每次测量前，在计时之外恢复同一起点（容差 1 点），最多 `max_reset_swipes=1–5` 次，可先点击 `reset_identifier`。只计上滑区间，之后验证内容移动；日志通过本次随机 measurement_id 绑定预热和记录次数，准备、恢复与查询不计入性能窗口。

`hitch_collector=signpost` 为默认；`application` 需 iOS 26+ 和对应精确指标绑定，不因 API 较新而自动切换。Debug Lab 仅在校准子进程环境中注入每次 3×250ms 阻塞，回读实际计数。正常/注入各至少两个样本；正常最大比例 ≤0.05、注入最小比例 ≥0.1、每样本至少 3 个卡顿且累计 ≥500ms 才为 usable，否则保留 rejected。阈值验证已知刺激，不能作为业务 SLO；少检一个也拒绝，不自动重试至通过。

校准绑定设备、OS build、Xcode build、App 安装标识/版本摘要、Runner 内容、采集器、目标/起点/重置及 bindings，7 天有效。重装、升级、修改配方或 Runner 后须重新校准。测量规格显式增加 `calibration_id` 才使用校准；执行前后核对范围。仅可改变 hitch_ratio 的缺帧/采集器待校准质量，不能修复帧计数、其它指标或跨 App 继承结论。

Web 客户端性能页支持校准、状态/证据查看、启动录制计划示例、滚动测量 JSON、冻结来源的离线门禁。usable 校准卡可直接生成带 ID 的测量；rejected 保留检查失败原因。命令行 rejected 返回非零退出码。

已有 xcresult 的来源规格使用 format=xcresult，并明确 path、app_id、device_id、test_id、configuration_id、bindings；只接受一个匹配的 test/device/configuration，指标重复、单位变化、缺数据均拒绝。

## 导入现有记录

```bash
iatest mobile import-metrics measurement-source.json --project demo
iatest mobile measurements --project demo
```

`.trace` 来源规格：

```json
{"format":"trace","path":"/path/to/recording.trace","platform":"ios","app_id":"com.example.app","target_pid":777,"run_number":1}
```

框架先读目录白名单，检查实际平台、目标 PID 和可用表，再只导出对应进程表；不枚举导出所有表。当前解析 `sysmon-process`、`activity-monitor-process-live`、`activity-monitor-process-ledger` 的明确工程类型，优先选择有效的 sysmon-process。XML 的 ID/ref 在表内解析，拒绝重复 ID、循环引用、类型不符、DTD 和实体；取原始数值及工程单位，不从格式化显示文字猜数值。

Animation Hitches 的上述两张帧表也在白名单内，关联后只保存目标区间及质量。设备进程统计 trace 的导入须显式增加 `trace_scope=device_process_statistics`；默认 process 范围拒绝 TOC 没有目标 PID 的 trace，不能把设备范围混成单进程来源。该选项只用于 format=trace。

已经导出的 XML 使用 `format=xctrace_xml`，必须指定实际 `platform` 和 `target_pid`。只有 XML 时，设备平台/App 来源属于调用方声明，会随记录保留该信任边界。声明为 macos 的数据不能通过 iOS 离线门禁；不会把 macOS 宿主实测改名为 iOS 真机实测。

其他已测量数据可使用以下明确的交换格式，来源规格为 `{"format":"measurements_json","path":"/path/to/measurements.json"}`：

```json
{
  "contract_version": 1, "platform": "ios", "app_id": "com.example.app",
  "source": {"provider": "custom", "device_id": "replace-with-actual-device", "os_version": "replace-with-actual-version", "build": "build-2", "test_id": "launch-check"},
  "measurements": [
    {"metric": "ios_launch_ms", "unit": "ms", "values": [900, 1200]},
    {"metric": "memory_footprint_kb", "unit": "KiB", "values": [19000, 21000]}
  ]
}
```

数字仅演示格式，不能作为 App 实测或门禁依据。provider 可为 xctest/instruments/custom，但标签不是独立认证；此处不是原始 xcresult/CSV 的通用自动解析器，需要调用方先明确其测量语义与单位。

| 指标 | 单位 | 语义 |
|---|---|---|
| cpu_percent | % | 指定进程的 CPU 百分比，可因多核超过 100 |
| memory_footprint_kb | KiB | 物理内存 footprint，非 Android PSS |
| resident_memory_kb | KiB | 驻留内存，非 footprint 或 PSS |
| ios_launch_ms | ms | 外部明确测量的启动时长 |
| frame_duration_ms | ms | 外部明确测量的帧时长 |
| frame_lifetime_ms | ms | 原始 hitches-frame-lifetimes 区间时长，经准确目标进程关联；独立于 XCTest 帧字段 |
| hitch_ratio | ratio | 已定义口径的停顿比例，范围 0–1 |

导入文件最多 16 MiB，单指标最多 10000 个数值；未知/不一致单位、重复指标、负数、非有限值和布尔值均拒绝。缺少指标保留缺测，不补零。

原始 trace 可能包含被采集进程的环境变量和凭据。解析过程只保留白名单平台/设备/PID、规范指标、摘要和来源指纹；不保存或向 AI/控制端上传原始环境与整份 trace。录制的临时目录在结束后清理，已有导入文件由调用方保管。

## 离线门禁与远程评估

```json
{
  "id": "ios-evidence-check", "kind": "client_performance",
  "spec": {
    "platform": "ios", "measurement": {"artifact_id": "ios-metrics-返回的ID"},
    "thresholds": {"max_ios_launch_ms": 1500, "max_memory_footprint_kb": 30000}
  }
}
```

```bash
iatest test import-plan ios-evidence-check.json --project demo
iatest test run ios-evidence-check --project demo
iatest test analyze 返回的运行ID --project demo
```

离线计划不接受 UI 计划、安装准备、iterations、warmup 或 startup，`fresh_ui_execution=false`。门禁使用已有样本最大值；来源采集不完整或请求的指标缺失时判定无法确定。`not_assessed` 只列出请求门禁的缺测指标，`unconfigured_metrics` 列出有数据但不在用户门禁范围内的指标；后者不自动构成失败。

控制台创建门禁会冻结测量来源指纹。远程评估可自动传送该任务冻结的规范测量资产，节点复核分块/整体摘要、类型/单位/样本数量及来源绑定；同 ID 对应不同内容时拒绝覆盖。不传送原始 trace 或设备凭据，也不把历史测量当作新执行。



## 质量与边界

标准 xctrace 仍可能出现 target_not_found、finalization_timeout；XCTest 原始零帧及旧 signpost 漏检不能视为有效样本。兼容副本、device_processes、原始 frame_lifetimes 与 application 校准都是显式路径，不自动回退、不补帧。校准只适用于其绑定的设备、系统、App 安装、Runner 和采集器范围。

注册 Metal 忙碌占比不是整机 GPU，设备电池状态不是 App 独占焦耳。任一门禁缺少有效指标时保持未评估，详见[能力边界](../reference/capabilities.md)。
