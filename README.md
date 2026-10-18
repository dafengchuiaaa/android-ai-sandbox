# Android AI Sandbox

面向 AI 的 Windows 本机后台 Android 沙箱原型。通过结构化界面节点读取、点击和输入，让 AI 操作独立 Android 实例，目标是不干扰用户的 Windows 前台工作。

An experimental, structured Android control environment for AI agents on Windows.

**阶段：研究与开发原型，尚未完成产品验收。** 本仓库开放控制代码、离线测试、读取器源码和底座研究，不是安装即用的成品。

## 当前有什么

- 独立 Android Emulator 实例的准备、启动、身份核验和受控停止。
- JSON 手机接口：`status`、`launch`、`observe`、`click`、`set-text`、`evaluate-search`；截图仅作显式诊断。
- 界面 XML 转结构化节点，以及节点选择、父链解析、观察与动作绑定。
- 可选 API35 `stable-direct` 读取器：两份 fresh 快照、完整 XML 与活动窗口检查、nonce/hash 和临时文件清理。
- 点击前完整 XML hash 必须仍与观察一致；文本输入有独立节点与焦点校验。失败不自动重放未知输入。
- Python 标准库离线测试、XML fixtures，以及研究结论和已知限制。

开发环境在 2026-10-18 完成过一次 API35 / F-Droid Basic 的真实闭环：启动 → Search → 输入 `Aurora` → 独立确认同字段文字与焦点 → 正常停机。该次会话约117.35秒，不能作为稳定性或性能排名。公开仓库没有附带私人原始运行记录，因此此处是维护者对开发验收的摘要，**不构成当前 checkout 的验收凭据**。详见[验证状态](docs/validation-status.md)。

后续已实现[AI逐步会话封装](docs/stepwise-agent-session.md)，11项离线检查通过。首次新实机尝试在launch后的两次读取中遭遇 `window_changed` / `unstable_tree`，86.111秒正常收尾，未点击或输入，AI多步目标未通过。另有[失败快照诊断候选](experiments/reader-failure-diagnostics/README.md)，已离线编译和完成4项协议兼容性检查，尚未安装或实机验证。失败原样保留，不扩大重试或放松完整XML保护。

## 先离线查看

Python 3.10+；这两组测试使用 fake ADB / XML fixtures，不需要 Android SDK 或设备：

```powershell
python -m unittest discover -s departments/zcode -p test_adb_control.py -v
python -m unittest discover -s departments/deepseek/tests -p test_content_adapter.py -v
python sandbox.py check
```

`check` 只做清点，当前公开版应返回“尚未验收”（退出码2）。公开版 `integration/acceptance.json` 有意保持未验收；不要改成 true 或跳过检查来开设备。

## 实机部署尚待完成

开发基线为 Windows、Windows PowerShell 5.1、Android SDK Emulator、Google APIs Android35 x86_64 系统镜像及自行取得的 F-Droid Basic APK。仓库不提供 SDK、Android 系统镜像、第三方 APK、AVD 数据或账号。

代码保留了开发时的固定 SDK 路径和版本绑定，以便审查原行为；尚未完成通用安装配置。新机器需要先完成受版本控制的可信路径配置、读取器构建、离线核验和新环境的有限验收。**不要直接照旧机器的路径运行启动脚本。** 入口仍默认 `legacy`；API35 与 `stable-direct` 都必须显式选择。

`stable_ui_read.jar` 是随源码提供的本项目读取器构建产物，仅包含 `classes.dex`，不是 Android SDK。源码和重建说明见[读取器构建](docs/build-reader.md)。新构建的字节/hash可能不同，需要重新审查绑定，不能删除 hash 校验。

## 目录

| 路径 | 内容 |
|---|---|
| `sandbox.py` | 顶层 CLI、响应校验和验收检查 |
| `departments/antigravity/` | 手机控制器、Java 读取器及 Python 读取合同 |
| `departments/deepseek/` | XML 内容适配器、有限 F-Droid 场景与 fixtures |
| `departments/zcode/` | ADB 包装和 fake-runner 测试 |
| `departments/workbuddy/` | Windows 实例准备和生命周期脚本 |
| `integration/` | 固定 profile、响应校验、离线清点与测试 |
| `docs/research/` | Windows / Linux 共享内核 / Cuttlefish 路线研究 |
| `tools/stepwise_session.py` | 外部AI逐步选动作、完整响应绑定和独立到期收尾 |
| `experiments/reader-failure-diagnostics/` | 未部署的失败快照元数据实验 |

目录名保留开发来源，方便追溯。公开源文件快照见[文件与 SHA256](docs/source-snapshot.json)。

## 接下来

1. 整理可移植的 Windows 配置和新 checkout 的验收流程。
2. 完成当前版本的 AI 逐步自主操作、真实联网刷新、人工前台输入三项验收。
3. 验证有限重复性、Windows 全机物理内存增量和受控常驻。
4. 在明确的 Windows 本地问题需要时，再评估替代底座。

Linux 原生容器路线和 Cuttlefish 的研究有参考价值，但切换 Linux 宿主不回答本项目的 Windows 部署问题；该路线目前归档，不是等待安装的前置条件。详见[底座研究](docs/research/android-host-options.md)。约1GiB已不作为硬性目标，本项目尚未证明新版本的资源改善或全天稳定运行。

## 许可与贡献

项目代码和原创文档采用 [Apache-2.0](LICENSE)，第三方内容保持其原有权利与许可。项目由 [dafengchuiaaa](https://github.com/dafengchuiaaa) 发起；Codex 协助整合、审查、测试和文档，其他 AI 辅助交付保留目录来源。此项目不代表 OpenAI、Google 或相关工具厂商的官方产品。见 [NOTICE](NOTICE)、[外部依赖](THIRD_PARTY.md) 与[贡献说明](CONTRIBUTING.md)。
