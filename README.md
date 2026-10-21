# Android AI Sandbox

面向AI/agent的Windows本机后台Android沙箱原型。通过结构化界面节点读取、点击、输入和独立回读，让agent操作自有Android实例，目标是不干扰Windows前台工作。

An experimental Windows-hosted Android control environment for AI agents. Apache-2.0.

## 2026-10-22：功能闭环与资源基线里程碑

- 外部AI逐步决策完成F-Droid Search → 输入Aurora → 同字段文字和焦点独立回读。
- 官方F-Droid仓库元数据刷新完成UI与同链网络证据核对。
- 对应开发运行完成正常Stop、自有实例退出和精确回退。
- 连续资源采样覆盖真实Worker/emulator/QEMU。空闲进程工作集合计约3.57GiB，任务峰值约3.78GiB；主要占用来自QEMU。

**还没有证明明显省内存，也不是安装即用或长期稳定的成品。** 上述是绑定开发版本的有限实机结果，公开checkout没有继承生产验收。详见[里程碑与测量口径](docs/milestone-20261022.md)、[验证状态](docs/validation-status.md)。

## 当前源码

- `sandbox.py`、`departments/`：正式JSON手机接口、Windows实例生命周期与API35读取器。
- `tools/stepwise_session.py`：外部agent逐步供决策；完整响应/观察hash与精确节点绑定，不内置固定UI动作序列。
- `experiments/host-supervisor/`：宿主协调器与Inspector的脱敏开发参考源码，尚需本地可信配置与版本manifest。
- `experiments/host-resources/`：Windows只读资源采样器，区分全机物理增量、进程工作集和私有提交量。

完整XML双快照、点击前观察保护、输入字段/焦点绑定、未知输入不重放、自有实例、正常Stop和精确回退保持。通用agent输入不依赖真人编辑器或人工声明。

## 离线检查与部署边界

Python 3.10+，在仓库根目录可运行不需要设备的检查，例如：

```powershell
python -B -m unittest integration.tests.test_delivery_gate
python -B -m unittest tools.tests.test_stepwise_session.PolicyTests
python -B sandbox.py check
```

公开`acceptance.json`保持未验收，最后一条应拒绝放行（退出码2），不是将开发环境成功复制成新机器PASS。部分历史实机回放测试需要另行提供原始fixture，否则应跳过。源码不附带SDK、APK、系统镜像、AVD、账户、聊天或原始设备日志。Windows/API35可信路径、依赖、组件hash及设备身份需要在本地配置并验收；参见[读取器构建](docs/build-reader.md)和[贡献说明](CONTRIBUTING.md)。

本轮代码来源与脱敏变换见[源码清单](docs/milestone-20261022-sources.json)；更早摘要/任务材料只代表当时版本，不覆盖本页。

## 下一步

围绕实测主要占用QEMU寻找一个有依据的最小减占用配置，比较内存、CPU和任务耗时；尚未实施。随后再处理配置可移植性和有限重复性。不重开真人输入支线，不把guest RAM当宿主占用，不预设1GiB门槛。Linux/Cuttlefish研究留档，不切换产品主线。

项目由[dafengchuiaaa](https://github.com/dafengchuiaaa)发起；Codex协助整合与验收，Gemini等AI辅助实现和验证。本项目不代表任何AI或平台厂商的官方产品。原创代码与文档采用[Apache-2.0](LICENSE)，第三方依赖遵循其原许可，见[NOTICE](NOTICE)和[THIRD_PARTY.md](THIRD_PARTY.md)。
