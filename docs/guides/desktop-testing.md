# 桌面测试

## 环境

桌面计划使用 `kind: desktop`，明确 `platform: macos|windows|linux`、`process_id` 和 `app_id`。Mac 使用 bundle ID，Windows 使用进程名，Linux 使用实际可执行文件名或绝对路径。目标进程必须已运行且暴露原生无障碍接口。

macOS 使用 AX，先编译驱动并授权无障碍；窗口截图还需要 macOS 14+ 和屏幕录制授权：

```sh
iatest test desktop-build /absolute/path/desktop-driver
export IATEST_DESKTOP_MACOS_DRIVER=/absolute/path/desktop-driver
```

Windows 使用系统 UIAutomation 和交互桌面；Linux X11 使用 AT-SPI、X11/XComposite。SSH 已连接不代表 Windows 交互桌面已可用。

## 计划与操作

```json
{
  "id": "desktop-save", "kind": "desktop", "contract_version": 1,
  "spec": {
    "platform": "macos", "process_id": 1234, "app_id": "com.example.app",
    "steps": [
      {"op": "fill", "target": {"identifier": "nickname"}, "value": "Example"},
      {"op": "click", "target": {"identifier": "save"}},
      {"op": "expect", "target": {"identifier": "status"}, "attribute": "text", "expected": "Saved: Example"}
    ]
  }
}
```

PID 和控件标识需要替换为实际值。目标按 `identifier/name/role` 过滤；操作要求新鲜、完整且唯一的原生查询。遍历有节点、深度和时间上限，不完整查询不能证明不存在。

| 操作 | 参数 |
|---|---|
| `find/click/fill/expect` | 明确语义目标；填字使用 `value` |
| `press` | `Enter/Tab/Escape/Backspace/Delete/Space`、方向键、Home/End/PageUp/PageDown |
| `right_click` | 唯一目标 |
| `drag` | 从目标中心到唯一 `destination` 中心 |
| `scroll` | 整数 `delta_x/delta_y`，各 -20 至 20，至少一项非零 |
| `screenshot` | 唯一语义目标的窗口内区域 |
| `visual_click` | 先观察的 `visual_anchor`，使用前重新校验 |

## GNOME Wayland

基础模式设置 `wayland_portal: true`，提供 AT-SPI 语义操作及门户授权按键。完整模式再设置 `wayland_backend: "gnome"`，支持右键、拖拽、滚轮、窗口截图及视觉点击。

```sh
iatest test wayland-setup --install --enable
```

新安装可能需要重新登录，再运行输出的 enable 命令。`--output DIRECTORY` 只准备文件；替换已修改组件须显式 `--overwrite`。运行需要系统 GIO、AT-SPI、GdkPixbuf 和支持 RemoteDesktop 的门户。

系统先请求键鼠/显示器权限，组件再请求所选 PID 的十分钟窗口读取权限。授权绑定调用者、进程启动身份和组件代次。截图只读取目标窗口，不读取整个桌面视频；窗口、焦点、几何、权限或身份无法确认时拒绝相应操作。系统撤销门户权限后，语义点击和填字也停止。

兼容基线是 GNOME 43.8、单显示器、100% 缩放；组件声明 43/44。其它合成器、版本、多屏与 DPI 配置需要独立验证，不自动转到 X11。

## 局部视觉

生成模型声明 `supports_images: true` 后，目标探索可以使用 `ui_visual_find`。返回坐标只有经过截图、视口及窗口绑定复核后才能输入，后续必须有业务断言。

图像每边最多 1024 像素、总计 524288 像素，最多采集三次并要求连续两张一致。窗口身份不同，即使像素相同也拒绝旧锚点。Mac 首次操作可直接截图，不依赖此前发送输入。

源码夹具与复验入口为 `examples/desktop_controls_lab.swift`、`desktop_controls_lab.py`、`desktop_visual_lab.swift`、`scripts/validate_desktop_interactions.py` 和 `validate_desktop_vision.py`。视觉复验会调用实际模型并消耗账户额度。
