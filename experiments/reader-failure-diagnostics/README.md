# 读取拒绝的快照诊断候选

实验源码，未装入开发设备，未通过实机验收，不替代当前 `departments/antigravity/StableUiRead.java` 或其JAR。

一次逐步AI演示在launch后的两次采集中先后得到window_changed和unstable_tree，未点击或输入，正常停机。原报告只有错误名，无法核对两份快照的包名、窗口和hash。旧恢复合同不接受unstable_tree；本实验不扩大该合同，也不为获得成功而忽略动态区域或重试输入。

此候选只在失败报告增加最多两条 `snapshot_diagnostics`：index、window_id、package、sha256、xml_bytes、taken_ms、cache_cleared。不附加完整XML或文字值。成功报告格式、两份完整XML相等检查、窗口/包名检查、300ms间隔、已有null-root轮询限额、退出码与finally清理保持原样。差异见diagnostics.patch，来源hash见source-provenance.json。

新增元数据能区分实际窗口/包切换与同窗口XML变化，但仅凭hash仍不能定位具体变化节点；不据此宣称已经找到应用抖动的根因。诊断字段不作为允许输入的依据。

离线兼容性检查：

```powershell
python -m unittest discover -s experiments/reader-failure-diagnostics -p test_diagnostics_compat.py -v
```

编译使用与现有读取器相同的Android35 SDK、Java source/target8、D8 min-api26；步骤见../../docs/build-reader.md，将输入源码换成本目录文件、输出放在独立build目录。实际编译结果见offline-build.json，不等于设备运行通过。后续若要实测，须先审查新产物hash、Python/JAR绑定、版本验收与新的有界范围；不得覆盖旧运行或删除一次锁。
