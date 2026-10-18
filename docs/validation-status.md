# 验证状态与证据范围

截至2026-10-18，维护者开发环境的最新有限实机：Android35 Google APIs x86_64 revision9 / stable-direct，单个 F-Droid Basic Search→Aurora 输入→同字段独立文字/焦点回读，正常停止；约117.350秒。10次读取最终成功，其中3次在原单读两次采集限额内恢复。未出现未知输入重放。

该开发运行绑定的 phone.py SHA256：`69b34d440e2b597b1361c6ae6410624aef2b826b2d1deb12572b97c9954bdd23`。开发用本地 F-Droid APK 为8753583字节，SHA256：`bbc4a34a7f65b200f9e40c4c74af4745de5cdbff45752ac0172916deb9f3c97d`。APK不附带；本地包hash本身也不是新设备安装内容的证明。

公开版只保留整理后的摘要，原运行文件含开发环境路径与日志，不原样上传。读者可以检查源码和离线 tests，但不能据此独立复核完整实机时序。因此公开 `acceptance.json` 不承接开发环境的 PASS，顶层 `sandbox.py check` 应 fail closed。

| 范围 | 状态 |
|---|---|
| 开发环境一次 F-Droid 读取/点击/ASCII 输入闭环 | 维护者实测成功，当前公开 checkout 未独立验收 |
| 启动后读取拒绝恢复 | 修正 runner 离线通过；该次实机未触发对应分支 |
| stale XML mismatch 落盘 | 离线验证；该次实机未触发 |
| 当前版本 AI 逐步自主操作演示 | 待验收 |
| 应用真实联网刷新 | 待验收 |
| Windows 人工前台并行打字 | 待验收 |
| 中文输入、更多应用、全天常驻 | 待验证 |
| 当前版本资源改善 | 未测，不声称达到约1GiB |
| Cuttlefish | 仅研究/前置核验，未启动或完成任务 |

当前硬件/环境中的单次成功不能推导跨机器兼容、长期可靠性或最优底座。初次公开后的离线实际运行结果另见 `docs/offline-verification.json`。
