# 88首个AI逐步实机：启动后读取拒绝，未进入输入阶段

北京时间2026-10-18 19:38:51–19:40:17，86.111秒。新的逐步封装使用已验API35/stable-direct生产工程，独立worker负责360秒总期限与40秒收尾；原84一次锁不动。新增封装11项离线检查实际通过，源码与计时范围已在开跑前固定。

主控基于实际启动器身份检查作出一次launch决策，正式CLI启动F-Droid一次。随后同次读取两次采集先后返回window_changed、unstable_tree，read elapsed5.2437秒；CLI以driver_timeout拒绝。没有获得可供节点选择的成功观察，因此点击0、set-text0，未知输入false；不能叫AI多步演示成功。未自动observe、重放launch或为通过而修改恢复白名单。

独立核对旧84严格恢复分类器也会拒绝此次混合错误，原因是第二次unstable_tree不在其允许范围。不是把已允许的恢复分支漏判成实机通过，也不自动扩大既有合同。

所有nonce的jar校验/清理命令符合原限额，清理rc0。正常Stop后模拟器自有-kill/-sleep20辅助进程短暂存在，原settle限额内自然退出；没有按名字杀进程或kill-server。最终ADB设备和CIM模拟器数组均空，112项生产源码与封装哈希前后相等，外层timeout0。详independent-audit.json；原始CLI失败、决策理由、stdout/stderr、时序及停机文件保存在session目录，未删锁覆盖。

当前三产品门仍false，生产receipt未修改。公开摘要位于公开仓库docs/stepwise-validation.json，不上传私人原始日志。

## 已继续完成的下一项

检查了现有Java collector：两次快照不一致时原报告只给错误名，没有分别保存window/package/hash元数据。已在独立experiment加入失败-only snapshot_diagnostics，不改成功报告、完整XML/窗口/包名比较、次数、sleep、退出码和cleanup。使用现有Android35/JDK/D8实际编译成功，4项协议兼容检查通过。候选未覆盖当前Java/JAR/phone，未安装或再开设备；构建取证见../reader-diagnostics-r1，公开源码见experiments/reader-failure-diagnostics。

下一阶段是评审这个精确诊断候选及其部署/版本绑定，给一次新的只读诊断范围；不是重复当前失败会话、绕过guard或把论文式研究当功能完成。需要的包名/窗口/hash证据补齐后，才能针对实际原因处理读取重复性。
