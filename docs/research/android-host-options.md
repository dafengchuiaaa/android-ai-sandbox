# Windows 后台 Android：底座研究与取舍

研究目标是给 Windows 用户提供可由 AI 操作的独立后台 Android 环境，同时保留前台工作体验。此文记录已有工程经验与未跑通路线，供其他项目参考；没有宣称发明新的虚拟化或 Android 容器技术。

## 路线区别

| 路线 | 宿主与内核 | 对本项目的意义 |
|---|---|---|
| 无窗口 Android Emulator | Windows 虚拟化上的独立 Android guest | 当前已能完成一次有限任务；继续验证产品能力与全机开销 |
| Waydroid 等 Linux 容器 | Android 用户空间使用宿主 Linux 内核、namespaces 等机制 | Linux 原生已有直接路线；不能把它的优势不加区分套到 Windows |
| Windows 上 Linux VM / WSL2 加容器 | 外层仍有 Linux 虚拟机，容器共享其中的 Linux 内核 | 可研究，但必须计入额外宿主层、部署和维护成本，未实测 |
| Cuttlefish | 使用可用的 Linux/KVM 宿主和对应 Android guest | 独立虚拟设备路线；当前未部署，不是共享内核容器的同义词 |

Waydroid 用 Linux namespaces 运行 Android 容器，宿主需支持 Binder 及 ashmem 或 memfd。共享内核减少一层独立 guest 内核的需要，但不会自动消除 Android 服务、应用、驱动支持与界面适配工作。[Waydroid概览](https://docs.waydro.id/)、[内核要求](https://docs.waydro.id/debugging/getting-essential-information)。WSL2 在受管理虚拟机中运行 Linux 内核；它与原生 Linux 宿主不是同一成本模型。[Microsoft架构说明](https://learn.microsoft.com/en-us/windows/wsl/compare-versions)。

## Cuttlefish 前置核验结论

2026-10-18一次实际 Windows 只读盘点中，`wsl --status`、`wsl --list --verbose`、`wsl --version` 都返回未安装 WSL；Windows可选功能状态因当前令牌未提升而未读到，记unknown。没有安装Linux、启动发行版、下载镜像或启动Cuttlefish。没有确认可用的Linux/KVM宿主，因此当时无法开展同任务对照。

官方Cuttlefish步骤要求可用KVM、宿主包、同build的guest镜像与host package。其常规ADB支持不等于支持Emulator的`emu avd name`控制台身份查询。[AOSP启动说明](https://source.android.com/docs/devices/cuttlefish/get-started)。

检查本项目代码还发现：设备serial被限定为Emulator偶数端口、状态绑定AVD/image路径、stable-direct只接受固定API35 profile。因此“换一个镜像照跑”不成立；需要新的生命周期身份适配，同时保留原读取/输入保护。精确API35 Cuttlefish build及同build host package没有固定，不应拿latest版本充当同版本比较。

## 可复用的比较方法

1. 固定APK hash、安装内容、初始数据/索引、现场显示/density、语言和IME；记录精确build、API/ABI及系统变体。
2. 每个底座都证明自有实例身份、创建范围及受控停止；用等强度适配而不是取消校验。
3. 保持同读取合同：每读≤20秒、设备命令≤8秒、最多两次采集；两份fresh且清缓存、间隔≥300ms、完整XML/window/package一致。
4. 使用同一个有限F-Droid任务；点击保持完整fresh XML hash匹配，输入保持精确节点与焦点绑定，未知输入不重放。
5. 单次失败/成功保留为案例；Google APIs与AOSP userdebug等混杂变量必须披露，不能据一个样本给稳定性/速度排名。
6. 资源测量计入Windows全机物理增量；VM或容器限额、guest RAM、单进程工作集不能代替这一指标。

## 当前取舍

项目保持 Windows 本机落地主线。Linux/Cuttlefish探索归档：不是判定其没有价值，而是另找Linux宿主不能直接解决本项目的Windows部署约束。优先验证AI逐步操作、真实联网刷新、用户前台输入、全机资源和受控常驻。替代底座只有能回答明确的Windows本地问题时才重新评估。
