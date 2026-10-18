# 重建 API35 读取器

输入源码：`departments/antigravity/StableUiRead.java`。开发构建使用 Android35 `android.jar`、Android build-tools 34.0.0 的 D8，Java source/target 8，D8 min-api26；实际读取合同仅在显式 API35 profile 开启。使用者自行取得官方 SDK 和合适 JDK，不下载到本仓库。

在仓库根目录，使用 Windows PowerShell 5.1。下列路径由使用者填入本地可信安装位置；先确认 `build/reader` 不含需要保留的数据：

```powershell
$taskJdk = 'C:\Path\To\JDK\bin'
$taskSdk = 'C:\Path\To\Android\Sdk'
$taskBuild = Join-Path (Get-Location) 'build/reader'
New-Item -ItemType Directory -Path "$taskBuild/classes", "$taskBuild/dex" -Force | Out-Null
& "$taskJdk/javac.exe" -encoding UTF-8 -source 8 -target 8 -classpath "$taskSdk/platforms/android-35/android.jar" -d "$taskBuild/classes" departments/antigravity/StableUiRead.java
if ($LASTEXITCODE -ne 0) { throw 'javac failed' }
& "$taskJdk/java.exe" -cp "$taskSdk/build-tools/34.0.0/lib/d8.jar" com.android.tools.r8.D8 --lib "$taskSdk/platforms/android-35/android.jar" --min-api 26 --output "$taskBuild/dex" "$taskBuild/classes/StableUiRead.class"
if ($LASTEXITCODE -ne 0) { throw 'D8 failed' }
python -c "import zipfile; z=zipfile.ZipFile('build/reader/stable_ui_read.jar','w',zipfile.ZIP_DEFLATED); z.write('build/reader/dex/classes.dex','classes.dex'); z.close()"
Get-FileHash -Algorithm SHA256 "$taskBuild/stable_ui_read.jar"
```

这是重建步骤说明，本次公开整理没有再次执行 Java 构建。ZIP 时间戳、JDK/D8版本可能令产物hash不同；不宣称逐字节可重现。不能直接覆盖已绑定 JAR 或删除完整性检查；先审查新产物，再同步更新代码绑定和验收记录。开发快照的读取器源/产物hash见 `docs/source-snapshot.json`。
