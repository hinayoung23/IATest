# Android 开发验收 App

这是框架自有的调试夹具，包含昵称持久化、多触点计数、输入法字段、列表动画、剪贴板读回、WebView 与 Debug SDK 观测。默认包名为 `dev.iatest.mobile.lab`；构建器也可生成独立 companion 和分包夹具，不覆盖业务 App。

源码随包提供，APK、构建目录与签名密钥不进入发布包。只在测试设备安装。WebView 启用调试和明文回环 HTTP，仅绑定设备 `127.0.0.1:18739`；不提供公网服务。SDK 仅用于调试观测，不用于证明任意业务 App 的测量精度。

v8 另提供显式 Intent 的受控 TCP 探针，供自有临时 VM 的 UID 防火墙验收：只接受32位十六进制操作标识、127.0.0.1/::1及18750/18751两个固定端口，连接/读取各2秒、响应最多256字节。仅写自有私有结果文件，返回实际 UID/PID 和响应；没有这些参数时不运行，不接受任意网络目标或业务动作。

使用已安装 Android SDK/JDK 与框架已有的开发密钥构建：

```bash
python scripts/build_android_lab.py \
  --keystore /path/to/existing/development.keystore \
  --output /private/tmp/iatest-android-lab --version-code 1
```

该脚本复用框架开发密钥格式，不创建或替换签名。覆盖安装必须沿用设备已安装版本的签名；签名不匹配应停止。全新安装会清除同包名历史数据，执行前确认目标是自有夹具。验收脚本与真实环境边界见 `docs/android-completion.md`。
