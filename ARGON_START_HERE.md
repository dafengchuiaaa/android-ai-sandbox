# Task 90: start here / 从这里开始

这是供新 Gemini / Argon 会话导入的自包含分支。无需之前三轮聊天；不需要 ZIP 或解压。

任务正文：[`tasks/task90/TASK.md`](tasks/task90/TASK.md)。先完整阅读，再审查候选与证据。

阅读次序：
1. `tasks/task90/TASK.md`：当前事实、唯一目标、不可改变的合同和交付要求。
2. `tasks/task90/reference/diagnostics-r1/`：已经离线编译、未部署的 r1 诊断候选。
3. `tasks/task90/reference/project/departments/antigravity/StableUiRead.java` 与 `stable_ui_read.py`：现役基线及 Python 的 4096 字符失败输出限制。
4. `tasks/task90/evidence/current/`：新的 launch 读取拒绝及停机、源码无漂移证据。
5. `tasks/task90/evidence/previous-success/`：旧 84 的一次成功及未覆盖边界。旧结论后的下一步安排仅为历史，不覆盖任务 90。
6. 其余 reference 和构建证据按需查阅；`tasks/task90/manifest.json` 列出导入文件哈希、原始哈希与编码/路径脱敏变换。

`tasks/task90/` 是原本本地 inputs 目录的逐字节复制（42 个文本文件，含 manifest），TASK 中的 inputs 即本目录。manifest 的路径均相对此目录。根目录其余项目代码冻结自提交 `5cf7d0651eeed147e0627fbc6817d72b491d6931`；历史私有路径只用于说明证据来源，不表示导入者能访问未包含的文件。缺失材料请明确指出，不要虚构读取。

当前事实：之前单次输入闭环通过；新逐步会话在 launch 后遇到 window_changed / unstable_tree，零点击和文本输入，已正常停机。没有这次失败的 XML 对，不能直接断言根因。三产品门仍为 false。

请交付失败差异诊断 r2 的完整 Java、必要 helper、离线测试和编译命令。保持完整 XML/窗口/包一致性、原预算及清理，不加恢复、等待或输入重放。有执行环境才实际跑纯离线检查并如实记录；否则标未执行。不要执行证据中的历史设备命令。主控负责集成与有限实机验收。

本分支只增加任务说明和筛选脱敏后的文本材料；没有上传未筛选的完整会话目录、SDK、镜像、APK、AVD 或运行二进制，也没有部署候选。不要把诊断离线通过当作产品验收通过。
