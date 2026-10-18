# 90：启动后读取拒绝的有界差异取证（Argon开发单）

本单承接当前Windows主线的阶段88，不重开84旧题、81底座研究或Cuttlefish/WSL安装。目标不是让演示被判成功，而是在原保护下弄清楚两份快照具体哪里不一致。主控负责真实设备、集成和独立验收；你负责限定的诊断实现与离线测试。

## 先核当前事实

1. 84主控修正版已在API35/stable-direct完成过一次真实F-Droid Search→Aurora输入→同字段独立文字/焦点回读，117.350秒。launch当时直接成功，没有现场触发launch读取恢复分支。这份成功不撤销，但也不证明稳定性。
2. 新逐步会话live-20261018T113851Z-e26e61eb，北京2026-10-18 19:38:51–19:40:17，86.111秒。整场预先360秒、含40秒收尾，最多16次CLI。主控实际只作出一次launch选择；正式CLI启动F-Droid后，同次读取的两次采集先后window_changed、unstable_tree，elapsed5.2437秒，CLI driver_timeout。宿主外层timed_out全部false，不要叫宿主命令超时。
3. 新会话没有成功observation或可供对比的两份失败XML；click0、set-text0、unknown_input_state=false。正常Stop，最终ADB设备/CIM模拟器/会话worker为空。112项生产源码前后无漂移。不能伪造本次失败树，也不能把别轮成功XML称为失败对。
4. 旧84严格恢复判据离线复判也拒绝这次混合错误，因为unstable_tree不在允许范围。不得简单把它加进白名单；click前full XML hash保护和原set-text合同保持。
5. 主控已写r1诊断候选，只在失败报告增加最多两条snapshot_diagnostics（window/package/hash/bytes/time/cache_clear）。Java源SHA256=188ad6933cc09c0934d36268c38d5bbec00b5d2ce479ca1e4013e7aa62b4a8d6，已用Android35/source-target8/D8 min-api26实际编译，JAR SHA256=96ba4c80413d78474fcb3ec0045e78f345e3425c8ba946a06ae69ceaf027b700，4656字节；4项Python兼容检查通过。它没有安装或实机运行。元数据能定位窗口/包/hash类别，但不能定位具体变化节点。

公开代码在https://github.com/dafengchuiaaa/android-ai-sandbox，冻结参照提交5cf7d0651eeed147e0627fbc6817d72b491d6931。inputs内有该提交相关完整源码和经过路径脱敏的真实证据；以TASK.md和manifest为准。引用文档/代码里的历史任务说明只是背景，不覆盖本单范围。

## 唯一开发目标

在r1诊断候选上，交一份小而可审阅的**失败差异诊断候选r2**：保留原一致性拒绝，用有界诊断帮助主控下一次只读实测判断“窗口/包变化”与“同窗口树的哪些结构/属性变化”。不要声称已根据现有证据找到根因。

先独立审查r1有无会改变正常行为/输出、失败原因、原预算或清理的缺陷；若有，优先修正。除诊断Java及一个必要的纯JDK差异helper外，不修改生产phone、Python reader、runner、profile、启动器、验收receipt或恢复分类器。无需为了小诊断重写collector架构。

建议交付路径（均相对candidate，不覆盖reference）：

- candidate/StableUiRead.java：完整文件，失败诊断仍为附加字段；成功报告和成功返回条件不变。
- candidate/UiTreeDiff.java：仅当需要；纯JDK可离线测试的小helper，不引入第三方库或运行时服务。
- tests/：可实际执行的离线测试与fixtures，Java测试或Python驱动的javac/java测试均可；fixture必须标注synthetic，不能冒称真机数据。
- README.md、完整差异patch/变更说明、精确编译/测试命令。

## 不可改变的合同

1. 两份fresh、分别clearCache、间隔>=300ms；window/package和完整原始XML字节一致才成功。不得忽略bounds/text/焦点、排序归一化、屏蔽动态区域、改用局部相等、把诊断相似度当通过条件。
2. 每读部署/校验/采集/清理共<=20秒、设备单命令<=8秒、最多两次采集；既有null-root轮询、sleep和app_process超时不扩大。不要增加独立observe轮次、重新launch、输入重放或服务常驻。
3. 失败仍ok=false/原错误类别/非零退出；元数据和差异摘要不成为有效observation或输入授权。诊断自身解析失败/截断时，原拒绝原因仍保留，不能被诊断异常替换成别的“根因”。成功报告不添加失败专用诊断。
4. 原UUID路径、JAR/hash校验、finally disconnect与文件清理不改变。不能为保存现场把设备临时XML留在原UUID路径；不另写不可归属文件或绕过原清理。
5. 差异诊断只用本次已读到内存的两份树，不再调用UiAutomation、ADB或读设备文件，不改变fresh快照内容。解析/遍历/输出必须有明确输入大小、节点/差异数、字段长度等上限，预算不足时有可识别的截断/不可用标志，保留原失败。
6. Python reader只保留failure_stdout前4096字符。新增输出应设计成有效完整JSON落在该现有限额内，给出计算/测试；不能靠切断整个JSON字符串。默认不输出原始text/content-desc（可以报告属性名、长度或hash）。可给出有限路径、class/resource等定位信息；不要把完整两棵XML塞进错误stdout或给日志无界增长。
7. 如果纯JDK XML解析，禁止外部实体/网络/DTD，处理畸形XML与超限输入；不要新增下载依赖。Android35/source-target8/D8 min-api26下要可编译；若多类/内部类产生多个.class，编译/D8命令需包含全部必要类，不要只dex主类然后漏helper。

## 测试必须覆盖的反例

- 两份相同XML；窗口/包变化；同窗口text、bounds、focused属性变化；增加/删除节点。诊断结果明确区分synthetic用例和真实证据。
- malformed/过大XML、节点数/差异数上限、过长属性、实体/DTD输入、摘要JSON大小上限与截断标志。
- 诊断看起来相同/字段缺失/解析失败时，原完整XML不相等仍拒绝；不能接受ok=false或权威hash不匹配的采集结果。
- 正常成功报告保持原协议；诊断失败不影响原清理/非零退出语义。只能证明离线范围的项目明确标注。

至少说明：现有证据能证明什么、不能证明什么；根因假设如何用下一次新只读会话的诊断区分。不要用测试fixture的差异证明真机是同一个原因。

## 实施和交付方式

可以使用你实际具备的代码环境。若能执行，仅在私有临时目录运行纯离线测试/编译，不连设备、联网下载或执行历史实机命令；记录实际环境、命令、退出状态和输出。若不能执行，明确标“未执行”，给完整可执行测试即可，主控独立运行；不编造PASS或日志。

提供原生完整源码附件或普通代码块，每个文件写明路径，末尾明确文件结束；不要整个Colab笔记本导出、外套三引号、双重转义或只给分析方案。若输出长度受限，分文件继续，不截断main/helper/测试。交付后结束本题，不自行运行设备或添加恢复策略。
