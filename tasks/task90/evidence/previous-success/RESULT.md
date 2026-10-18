# 单个F-Droid真实输入闭环通过；实例已停止

2026-10-18 Asia/Shanghai。实机会话live-20261018T102038Z-27ad9363，北京时间18:20:38–18:22:35，117.350秒。**当前API35/stable-direct已完成F-Droid启动、唯一Search标签父链点击、一次Aurora输入和同字段独立精确文字/focused回读。** 正常停机，最后ADB devices与CIM模拟器进程清单均为空。没有重复launch、刷新后重试或输入重放。

## 版本与作者交付的边界

本次运行的是主控修正后的runner，SHA256 155a150c44e0bc9edb9fc8d10fef55ffc2548fa97dc4bf68e4db8a9899d259f8。不是未经修改的Argon原交付。原补交104436字节，作者明确未执行；主控实跑原新增5测试有4失败。修正argv长度及后续错误停止/证据自洽检查7处后，作者5、旧mock5、独立5共15项通过。原版、诊断与修正版分别留存，详见../../intake/task84-complete-review/RESULT.md和delivery/controller-changes.json。

生产phone仍69b34d440e2b597b1361c6ae6410624aef2b826b2d1deb12572b97c9954bdd23。生产109项本轮基线和新增3项在前、后及最终共112项无漂移；本单未改phone/reader/Java/jar/adapter/launcher/validator/receipt。默认legacy，本次显式profile api35-google-x86_64、read-backend stable-direct。三产品门仍false。

wrapper明确真实路径并补核3份新增文件，使用新的独占会话锁；旧会话锁保持。start/status/stop统一PS5.1，serial从本次state读取，PID创建时间/模块/AVD/API/ABI现场核验后才操作自有实例。start命令38.371秒返回；单次耗时不是性能基准。

## 实际调用与输入证据

| 项目 | 本次实际情况 |
|---|---|
| 自有实例start | 1次 |
| F-Droid launch | 1次，返回成功 |
| 独立readiness observe | 1次，目标包org.fdroid.basic |
| Search click CLI | 1次，完整XML/hash保护后发送导航tap，返回成功 |
| set-text | 1次，value=Aurora，后端verified=true |
| 独立文字回读 | 1次，text=Aurora且focused=true |
| 正常Stop | 1次，最终无设备/模拟器进程 |

readiness observation_id=dc23ae585c474fa0ff8c0c9988eeae5f21fca85fa8f8ae65f4e0fbbd44241d4d。Search标签自身clickable=false，requested node为dc23ae585c47:R.0.0.0.0.0.0.0.0.0.1.0.1.0.0；冻结adapter解析真实父链到可点击祖先dc23ae585c47:R.0.0.0.0.0.0.0.0.0.1.0.1。计划tap=(360,1152)与该祖先真实bounds中心一致，未猜坐标、未绕CLI输入。

点击返回后的新观察和额外fdroid-search-fresh都出现唯一空EditText，原bounds=[112,56,608,168]，path=R.0.0.0.0.0.0.0.0.0.0.1.0.1.0。set-ascii-once.json记录input_text/readback_text均Aurora且verified=true；ascii-independent-readback.json和ascii-verification.json独立确认相同package/class/path/resource身份，文字Aurora、focused=true。输入后bounds=[112,56,704,168]，左/上/下锚点未变、右侧变化由原有focused resize合同允许，不是本轮放宽。最终回读完整hash=708e07aa73e005a8f14b39f94c72c4b9efdc57138a7d8b05c9c5349cb30ca200。

Search导航tap确认1次不等于所有底层输入事件只有1次；set-text内部有自身焦点/清理流程。此处统计CLI调用与业务导航，不虚构底层事件总数。

## 读取与收尾独立复核

10次读取、13次采集，10次最终读取全部成功，各1.6413–10.2598秒，单读≤20秒、设备命令timeout≤8秒、每读最多2次采集。3次首采集分别被unstable_tree、window_changed、unstable_tree拒绝，均在原同一次读取的第二次限额内恢复，未重放输入；失败原始stdout保留。不可将内部采集恢复混同为launch失败后跨调用readiness恢复。

10份最终proof的nonce、cache_clear=[true,true]、双快照≥300ms、window/package/双hash、UUID专属jar/XML清理均复核通过。13次采集的临时文件清理rc均0，所有外层命令无timeout。4份按完整hash保存的唯一observation均有对应有效proof并核验原始XML完整hash与本次serial/profile/backend绑定。4是唯一保存hash数，不代表只有4次读取。

Stop命令3.873秒，随后短命模拟器停机辅助进程自然退出；共用40秒收尾内最终devices为空、CIM数组=[]。未kill-server、未按名字杀进程。完整独立复核见final-verification.json、会话independent-evidence-audit.json和input-disposition.json。

## 本次没有证明的部分

- launch这次直接成功，launch_recovery.classification=not_needed_launch_ok；新增“launch后读拒绝进入独立readiness”分支仍只有离线验证，不因本次输入成功而改判现场已覆盖。
- 本次没有stale hash mismatch，83-r2新增current XML落盘分支仍未得到真实拒绝场景覆盖；不为了覆盖而主动制造新设备轮次。
- 这是单次、单应用、单ASCII值的有限闭环；不代表冷启动/读取长期重复性、任意IME/中文、多应用、联网刷新、人工并行输入、常驻或资源改善。
- 旧Settings与旧版本演示证据仍按原hash归档，不自动重绑定。三产品门仍false，Emulator仅可用参照，不能推断底座最优或能力极限。

本轮限定实机结束，无需为84再给Argon追加题目。已更新功能限制与下一阶段底座对照前置方案（CAPABILITIES-AND-NEXT.md）；不直接安装候选系统、不自动重复本会话。
