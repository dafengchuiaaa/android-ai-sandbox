# AI 逐步操作的有界会话

`tools/stepwise_session.py` 是实验性取证封装。AI 在每次调用之间读取真实返回、选择节点和写出理由；封装只执行显式请求，不内置 Search→输入的动作序列。不会从这个工具的存在推断 AI 实际操作成功。

## 合同

- Windows PowerShell 5.1、自有 API35/stable-direct 实例、正式 `sandbox.py` CLI；不提供任意 shell 或坐标输入接口。
- 整场预先固定360秒，含模型决策等待，预留40秒收尾；最多16次CLI，一次launch、一次click、一次set-text（值限Aurora）。单读≤20秒/单设备命令≤8秒/最多两次采集保持原合同。
- 每步请求有观察 hash、节点和理由，并绑定上一份完整响应文件的 SHA256。输入使用最新完整观察中的真实唯一节点；最终仍由 phone 的 fresh XML/节点/焦点保护裁决。
- click前还须容纳完整click+set-text+独立回读与预留收尾；不能因为剩余预算不足而切断一次正常复合输入后又重放。
- 任一调用错误结束会话；输入的次数在子进程前认领，不能因为超时、stale或响应缺失释放次数。初始字段必须唯一、空、非密码。
- 独立 worker 负责期限与正常Stop。没有下一条模型指令也会到期收尾。重复begin/worker均有独占认领，旧会话锁不得删除复用。
- 后端set-text成功还不算目标完成；必须再由外部AI请求独立observe，核对同path/package/class/resource、锚点、Aurora文字及焦点。

支持代码 `tools/stepwise_support.py` 来自已审查的限定执行与collector审计原语，来源 SHA256 `155a150c44e0bc9edb9fc8d10fef55ffc2548fa97dc4bf68e4db8a9899d259f8`。提取时移除了原固定场景的`run()`、`main()`与模块入口，只保留通用执行/校验方法；整场上限在新范围预登记为360秒。原主控修正包括严格返回码/collector字段和预算判据，不能把它当作未经修正的原模型交付。

## 使用范围

先运行纯离线检查：

```powershell
python -m unittest discover -s tools/tests -p test_stepwise_session.py -v
```

开发环境的有界验收使用单独的已验收工程、经审阅的源码manifest和私有证据目录。公开checkout的验收receipt仍未通过；此工具不让它自动获得实机运行权限，不给出绕过验收的命令。路径可移植性与公开checkout实机准入仍是后续工作。

接口为 `begin --project-root ... --adb ... --manifest ... --out ...`、逐步 `submit --out ... --action ... --reason ...`，输入还需完整observation/node和值。`finish`也需要外部理由。启动器的状态与观察写入其原限定运行目录，模型决策/完整响应/时序/收尾保存在指定私有证据目录；不把私人日志提交Git。

这是一次预先授权范围内的任务worker，不是定时自动化，也不会无限常驻。新会话的真实结果必须另行记录；离线测试或脚本自行生成的标签均不能冒充AI演示PASS。
