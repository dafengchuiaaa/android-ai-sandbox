#Requires -Version 5.1
<#
.SYNOPSIS
  Prepare the project-dedicated Android 15 AVD "ai-sandbox-api35-google"
  (API 35, x86_64, google_apis tag) inside the round4 runtime root.

.DESCRIPTION
  Round4 task 35 candidate (dispatch 35 / overview 33). Independent launcher
  pair for profile "api35-google-x86_64"; the old API33 baseline launcher and
  its AVD data are never read, written or repurposed.

  Frozen interface (overview 33):
    profile id : api35-google-x86_64
    AVD name   : ai-sandbox-api35-google
    image      : <SdkRoot>\system-images\android-35\google_apis\x86_64
                 (read-only; verified against source.properties metadata)
    AVD root   : <Round4Root>\avd            (process-scoped ANDROID_AVD_HOME)
    state      : <Round4Root>\instances\instance-<id>.json   (see start-api35.ps1)
    logs       : <Round4Root>\logs

  HARDENING (vs old prepare-baseline.ps1):
    * Path containment no longer uses a bare StartsWith prefix compare. Every
      write target is normalized with GetFullPath and must sit inside the
      approved root with a real separator boundary, and no component below the
      root may be a junction/symlink (reparse point).
    * The system image is verified by metadata (ApiLevel=35, Abi=x86_64,
      TagId=google_apis) and required files; missing or foreign content fails
      the run. Nothing is downloaded; the API33 userdata.img template is never
      copied (that would impersonate a different system).
    * Idempotent: an already-prepared AVD (config.ini AvdId matches) is left
      byte-for-byte untouched and the script exits 0. Unknown existing content
      is refused, never overwritten.
    * Writes only inside Round4Root. The shared SDK and the user profile
      .android directory are never written. No permanent environment variable
      is created.

  TEST SEAMS (offline verification only; they relocate the sandbox but never
  relax any safety rule):
    -SdkRoot      fake SDK root containing emulator\emulator.exe and
                  system-images\android-35\google_apis\x86_64
    -Round4Root   isolated runtime root for AVD/state/logs
    -EmulatorExe  executable whose existence is preflighted

.PARAMETER SdkRoot
  Android SDK root. Default: C:\Users\Administrator\AppData\Local\Android\Sdk

.PARAMETER Round4Root
  Round4 runtime root. Default: <script>\..\runtime\round4
  (script lives in departments/workbuddy/round4-launcher; one level up is
  departments/workbuddy, so this resolves to departments/workbuddy/runtime/round4)

.PARAMETER EmulatorExe
  Emulator executable to preflight. Default: <SdkRoot>\emulator\emulator.exe

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\prepare-api35.ps1
#>

[CmdletBinding()]
param(
    [string]$SdkRoot = 'C:\Users\Administrator\AppData\Local\Android\Sdk',
    [string]$Round4Root = '',
    [string]$EmulatorExe = '',
    [switch]$TestMode
)

$ErrorActionPreference = 'Stop'

# ------------------------- frozen identity -------------------------
$ProfileId = 'api35-google-x86_64'
$AvdName   = 'ai-sandbox-api35-google'
$ApiLevel  = 35
$Abi       = 'x86_64'
$TagId     = 'google_apis'
$ImageRel  = 'system-images\android-35\google_apis\x86_64'

# ------------------------- path helpers -------------------------
function Get-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-ReparsePointUnder([string]$RootFull, [string]$TargetFull) {
    $ancestor = Get-NormalizedPath $TargetFull
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            if ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $true }
        }
        $parent = Split-Path -Parent $ancestor
        if ($parent -eq $ancestor) { break }
        $ancestor = $parent
    }
    # True if any existing component strictly below RootFull is a junction or
    # symlink. GetFullPath does NOT resolve reparse points, so this check is
    # what stops "<root>\link -> C:\elsewhere" style escapes.
    $rootTrim = $RootFull.TrimEnd('\', '/')
    if ($TargetFull.Length -le $rootTrim.Length) { return $false }
    $rel = $TargetFull.Substring($rootTrim.Length).TrimStart('\', '/')
    $cur = $rootTrim
    foreach ($seg in ($rel -split '[\\/]')) {
        if ([string]::IsNullOrWhiteSpace($seg)) { continue }
        if ($seg -eq '.' -or $seg -eq '..') { continue }
        $cur = Join-Path $cur $seg
        if (Test-Path -LiteralPath $cur) {
            $item = Get-Item -LiteralPath $cur -Force
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                return $true
            }
        }
    }
    return $false
}

function Assert-InsideRoot([string]$Path, [string]$RootFull, [string]$What) {
    $full = Get-NormalizedPath $Path
    $rootTrim = $RootFull.TrimEnd('\', '/')
    $inside = $full.Equals($rootTrim, [System.StringComparison]::OrdinalIgnoreCase) -or
              $full.StartsWith($rootTrim + [System.IO.Path]::DirectorySeparatorChar,
                               [System.StringComparison]::OrdinalIgnoreCase)
    if (-not $inside) {
        throw ("REFUSE: {0} escapes the approved root {1}: {2}" -f $What, $RootFull, $full)
    }
    if (Test-ReparsePointUnder $RootFull $full) {
        throw ("REFUSE: {0} passes through a junction/symlink below {1}: {2}" -f $What, $RootFull, $full)
    }
    return $full
}

function Write-TextFile([string]$Path, [string]$Content) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    # UTF8 without BOM: config.ini content is plain ASCII here anyway
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

# ------------------------- resolve targets -------------------------
if ([string]::IsNullOrWhiteSpace($SdkRoot)) { throw 'ABORT: -SdkRoot must not be empty.' }
$SdkRoot    = Get-NormalizedPath $SdkRoot
if ([string]::IsNullOrWhiteSpace($EmulatorExe)) {
    $EmulatorExe = Join-Path $SdkRoot 'emulator\emulator.exe'
}
$EmulatorExe = Get-NormalizedPath $EmulatorExe

if ([string]::IsNullOrWhiteSpace($Round4Root)) {
    # script lives in departments/workbuddy/round4-launcher -> up ONE level
    # (to departments/workbuddy) then runtime\round4. Overview 33 freezes the
    # AVD/state/logs root at <project>/departments/workbuddy/runtime/round4.
    $Round4Root = Join-Path $PSScriptRoot ('..\runtime\round4')
}
$Round4Root = Get-NormalizedPath $Round4Root

$ProjectRoot = Get-NormalizedPath (Join-Path $PSScriptRoot '..\..\..')
$FixedSdk = 'C:\Users\Administrator\AppData\Local\Android\Sdk'
$FixedRuntime = Join-Path $ProjectRoot 'departments\workbuddy\runtime\round4'
if ($TestMode) {
    $testRoot = Join-Path $ProjectRoot 'integration\round4\tests'
    foreach ($testPath in @($SdkRoot,$Round4Root,$EmulatorExe)) { Assert-InsideRoot $testPath $testRoot 'offline test path' | Out-Null }
} elseif ($SdkRoot -ine $FixedSdk -or $Round4Root -ine $FixedRuntime -or $EmulatorExe -ine (Join-Path $FixedSdk 'emulator\emulator.exe')) {
    throw 'REFUSE: production SDK, executable and runtime paths are fixed; offline overrides require -TestMode'
}
Assert-InsideRoot $Round4Root $Round4Root 'runtime root' | Out-Null

$ImageDir  = Join-Path $SdkRoot $ImageRel
$AvdHome   = Join-Path $Round4Root 'avd'
$AvdDir    = Join-Path $AvdHome ($AvdName + '.avd')
$AvdIniPath= Join-Path $AvdHome ($AvdName + '.ini')
$ConfigIni = Join-Path $AvdDir 'config.ini'

# ------------------------- preflight: executables -------------------------
if (-not (Test-Path -LiteralPath $EmulatorExe)) {
    throw ("MISSING: emulator not found at {0}. Next step: point -SdkRoot at the real SDK location; do NOT install a new SDK from this script." -f $EmulatorExe)
}

# ------------------------- preflight: system image -------------------------
$required = @('system.img', 'kernel-ranchu', 'ramdisk.img', 'build.prop', 'source.properties')
foreach ($f in $required) {
    $p = Join-Path $ImageDir $f
    if (-not (Test-Path -LiteralPath $p)) {
        throw ("MISSING: system image file {0}. Next step: verify the API35 google_apis x86_64 image exists at {1}; do NOT download a substitute and do NOT switch to a Play Store image." -f $p, $ImageDir)
    }
}

# metadata verification: this must be the API35 google_apis x86_64 image
$props = @{}
foreach ($line in (Get-Content -LiteralPath (Join-Path $ImageDir 'source.properties'))) {
    if ($line -match '^\s*([^=#;\s][^=]*?)\s*=\s*(.*?)\s*$') {
        $props[$Matches[1].Trim()] = $Matches[2].Trim()
    }
}
$metaChecks = @{
    'AndroidVersion.ApiLevel' = [string]$ApiLevel
    'SystemImage.Abi'         = $Abi
    'SystemImage.TagId'       = $TagId
}
foreach ($k in $metaChecks.Keys) {
    $want = $metaChecks[$k]
    $got  = $props[$k]
    if ([string]::IsNullOrWhiteSpace($got)) {
        throw ("REFUSE: source.properties at {0} has no '{1}' entry; cannot confirm this is the {2} image. Refusing to build an AVD from unverified content." -f (Join-Path $ImageDir 'source.properties'), $k, $ProfileId)
    }
    if ($got -cne $want) {
        throw ("REFUSE: source.properties '{0}' is '{1}', expected '{2}'. This is not the {3} image; do not repurpose another image." -f $k, $got, $want, $ProfileId)
    }
}
Write-Host ("image metadata verified: ApiLevel={0} Abi={1} TagId={2} at {3}" -f $props['AndroidVersion.ApiLevel'], $props['SystemImage.Abi'], $props['SystemImage.TagId'], $ImageDir)

# ------------------------- idempotency guard -------------------------
if (Test-Path -LiteralPath $ConfigIni) {
    $existing = Get-Content -Raw -LiteralPath $ConfigIni
    # Multiline anchor: the file is a multi-line string, so a bare `$` would
    # only match the end of the whole file and every rerun would look foreign.
    if ($existing -match ('(?m)^AvdId\s*=\s*' + [regex]::Escape($AvdName) + '\s*$')) {
        Write-Host ("AVD '{0}' already prepared at {1}. Left untouched (no overwrite). Nothing to do." -f $AvdName, $AvdDir)
        Write-Host ("Run start-api35.ps1 to boot it.")
        exit 0
    }
    throw ("REFUSE: {0} exists but its AvdId is not '{1}' (unknown AVD data). Inspect it manually; this script never overwrites existing AVD content." -f $ConfigIni, $AvdName)
}
if (Test-Path -LiteralPath $AvdIniPath) {
    $iniText = Get-Content -Raw -LiteralPath $AvdIniPath
    if ($iniText -notmatch [regex]::Escape($AvdName + '.avd')) {
        throw ("REFUSE: {0} exists but does not point at this project's AVD. Inspect it manually." -f $AvdIniPath)
    }
    Write-Host ("Found stale {0} pointing at our AVD dir; recreating the missing .avd directory only." -f $AvdIniPath)
}

# ------------------------- containment assertions -------------------------
Assert-InsideRoot $Round4Root $Round4Root 'round4 root' | Out-Null
$AvdHome    = Assert-InsideRoot $AvdHome    $Round4Root 'ANDROID_AVD_HOME target'
$AvdDir     = Assert-InsideRoot $AvdDir     $Round4Root 'AVD data dir'
$AvdIniPath = Assert-InsideRoot $AvdIniPath $Round4Root 'AVD ini file'
$ConfigIni  = Assert-InsideRoot $ConfigIni  $Round4Root 'AVD config file'

New-Item -ItemType Directory -Path $AvdDir -Force | Out-Null

# ------------------------- userdata template -------------------------
# Only a template shipped inside THIS image may be copied. The API35
# google_apis x86_64 image on this machine ships no userdata.img; the emulator
# creates one on first boot. Copying the API33 template is explicitly forbidden
# (it would make a foreign writable data partition look like the new system).
$userdataTarget = Join-Path $AvdDir 'userdata.img'
$imageUserdata  = Join-Path $ImageDir 'userdata.img'
if (Test-Path -LiteralPath $imageUserdata) {
    if (-not (Test-Path -LiteralPath $userdataTarget)) {
        Copy-Item -LiteralPath $imageUserdata -Destination $userdataTarget -Force
        Write-Host "Copied userdata.img template from the API35 image directory."
    } else {
        Write-Host "userdata.img already present; left untouched."
    }
} else {
    Write-Host ("No userdata.img template inside {0}. The emulator creates userdata.img on first boot; no API33 file is ever copied." -f $ImageDir)
}

# ------------------------- build the AVD -------------------------
$imageDirSep = $ImageDir.TrimEnd('\') + '\'
$config = @"
AvdId=$AvdName
PlayStore.enabled=false
abi.type=$Abi
avd.ini.displayname=$AvdName
avd.ini.encoding=UTF-8
disk.dataPartition.size=2G
fastboot.chosenSnapshotFile=
fastboot.forceChosenSnapshotBoot=no
fastboot.forceColdBoot=yes
fastboot.forceFastBoot=no
hw.accelerometer=no
hw.arc=false
hw.audioInput=no
hw.battery=yes
hw.camera.back=none
hw.camera.front=none
hw.cpu.arch=$Abi
hw.cpu.ncore=2
hw.dPad=no
hw.gps=no
hw.gpu.enabled=yes
hw.gpu.mode=software
hw.gyroscope=no
hw.initialOrientation=portrait
hw.keyboard=yes
hw.lcd.density=320
hw.lcd.height=1280
hw.lcd.width=720
hw.mainKeys=no
hw.ramSize=1536
hw.sdCard=no
hw.sensors.light=no
hw.sensors.magnetic_field=no
hw.sensors.orientation=no
hw.sensors.pressure=no
hw.sensors.proximity=no
hw.trackBall=no
image.sysdir.1=$imageDirSep
runtime.network.latency=none
runtime.network.speed=full
showDeviceFrame=no
tag.display=Google APIs
tag.displaynames=Google APIs
tag.id=$TagId
tag.ids=$TagId
target=android-$ApiLevel
vm.heapSize=192
"@

Write-TextFile $ConfigIni $config

$avdIni = @"
avd.ini.encoding=UTF-8
path=$AvdDir
path.rel=$AvdName.avd
target=android-$ApiLevel
"@
Write-TextFile $AvdIniPath $avdIni

# ------------------------- summary -------------------------
Write-Host ""
Write-Host "AVD prepared:"
Write-Host ("  profile id    : {0}" -f $ProfileId)
Write-Host ("  name          : {0}" -f $AvdName)
Write-Host ("  ANDROID_AVD_HOME target : {0}" -f $AvdHome)
Write-Host ("  AVD dir       : {0}" -f $AvdDir)
Write-Host ("  system image  : {0} (API {1}, {2}, {3})" -f $ImageDir, $ApiLevel, $Abi, $TagId)
Write-Host ("  profile       : 720x1280, 2 vCPU, 1536 MB guest RAM, software GPU, no audio/camera, no snapshot")
Write-Host ""
Write-Host "Next step: run start-api35.ps1 (process-scoped ANDROID_AVD_HOME, accel gate, hidden boot, ownership + API/ABI verification)."
exit 0
