# 外部依赖与来源

本仓库开放本项目的控制代码和原创研究说明。Python 标准库、Windows PowerShell、Android SDK/ADB/Emulator、Android 系统镜像和 F-Droid 是外部依赖，分别遵循其自身许可及分发条款，不因本仓库采用 Apache-2.0 而改变。

| 依赖/资料 | 本仓库如何使用 | 是否附带其分发包 |
|---|---|---|
| Python / Windows PowerShell | 执行控制器、离线测试与本地生命周期脚本 | 否 |
| Android SDK / ADB / Emulator | 由使用者自行安装和配置 | 否 |
| Android Google APIs 系统镜像 | 开发环境参照，须由使用者自行取得 | 否 |
| F-Droid Basic | 有限实测目标；开发用 APK hash 见验证状态 | 否 |
| Android API / UiAutomation | 项目 Java 读取器调用的系统接口 | 不含 android.jar 或 Android 系统实现 |
| Waydroid / Cuttlefish 文档 | 研究中的引用与原文链接 | 不含其源码或镜像 |

`departments/antigravity/stable_ui_read.jar` 由项目 `StableUiRead.java` 经 javac 和 D8 生成；保留 Java 源码和构建说明。它调用设备上的 Android API；构建工具与 SDK 不打包进仓库。

`fixtures/originals` 中 XML 来自开发设备上的 F-Droid / Android Settings 界面层次；它们是测试输入，不是应用源码、APK或个人账号数据。`fixtures/synthetic` 为合成边界用例。界面中的应用名称与标签仅用于测试与说明。

初次公开整理未发现选入源码携带其他第三方版权/许可声明。该检查不替代未来新增代码的来源审查；贡献中引入第三方实现必须同时给出来源、版本和适用许可，不删除原署名。
