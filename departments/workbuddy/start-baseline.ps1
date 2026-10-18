#Requires -Version 5.1
<#
.SYNOPSIS
  Boot (or stop) the project-dedicated hidden Android emulator instance
  (AVD ai-sandbox-baseline) with explicit port selection and ownership checks.

.DESCRIPTION
  WB-02 hardening (review item 5):
  * Explicit port selection: an even port is chosen from a candidate list and
    must be free (nothing in `adb devices` AND nothing listening on loopback).
    The port is passed to the emulator via -port and reused by every later
    command; the serial is always emulator-<port>.
  * Ownership: after boot, `adb -s <serial> emu avd name` must report
    ai-sandbox-baseline. Unknown or foreign instances are only reported, never
    killed; `adb kill-server` is never called (the ADB server is shared).
  * Bounded timeouts everywhere; a try/finally guarantees that an instance THIS
    script started is cleaned up even when boot verification fails.
  * Uncertain shutdown result is reported as FAILURE (non-zero exit), never exit 0.
  * Existing AVD data is never erased (no -wipe-data, snapshots untouched).

  The process-scoped ANDROID_AVD_HOME points at departments/workbuddy/runtime/avd;
  no permanent environment variable is created and the user profile
  .android/avd is not touched.

.PARAMETER Port
  Even TCP port for this instance. 0 (default) = auto-select the first free
  even port from the candidate list.

.PARAMETER BootTimeoutSec
  Max seconds to wait for sys.boot_completed=1 (default 300).

.PARAMETER PollIntervalSec
  Seconds between boot polls (default 5).

.PARAMETER StateFile
  JSON file recording the instance identity (pid/serial/port/avd/logdir).
  Default: departments/workbuddy/runtime/instance.json

.PARAMETER Stop
  Stop mode: read the state file, verify ownership, shut down cleanly and
  verify the result. No boot.

.PARAMETER AutoStop
  Boot mode: after a successful boot check, stop the instance immediately
  (one-shot verification).

.EXAMPLE
  .\start-baseline.ps1                      # auto port, leave running
  .\start-baseline.ps1 -AutoStop            # boot, verify, stop
  .\start-baseline.ps1 -Stop                # stop the recorded instance
#>

[CmdletBinding(DefaultParameterSetName = 'Boot')]
param(
    [Parameter(ParameterSetName = 'Boot')]
    [int]$Port = 0,

    [Parameter(ParameterSetName = 'Boot')]
    [int]$BootTimeoutSec = 300,

    [Parameter(ParameterSetName = 'Boot')]
    [int]$PollIntervalSec = 5,

    [string]$StateFile = '',

    [Parameter(ParameterSetName = 'Stop')]
    [switch]$Stop,

    [Parameter(ParameterSetName = 'Boot')]
    [switch]$AutoStop
)

$ErrorActionPreference = 'Stop'

# ------------------------- fixed project paths -------------------------
$SdkRoot     = 'C:\Users\Administrator\AppData\Local\Android\Sdk'
$EmulatorExe = Join-Path $SdkRoot 'emulator\emulator.exe'
$AdbExe      = Join-Path $SdkRoot 'platform-tools\adb.exe'
$AvdName     = 'ai-sandbox-baseline'
$DeptRoot    = $PSScriptRoot                      # departments/workbuddy
$AvdHome     = Join-Path $DeptRoot 'runtime\avd'
$LogRoot     = Join-Path $DeptRoot 'logs'
if ([string]::IsNullOrWhiteSpace($StateFile)) {
    $StateFile = Join-Path $DeptRoot 'runtime\instance.json'
}

# ------------------------- helpers -------------------------
function Invoke-Native {
    # Windows PowerShell 5.1 raises a TERMINATING NativeCommandError whenever a
    # native command writes to stderr while $ErrorActionPreference='Stop' (even
    # with 2>$null; verified on this host). Drop to 'Continue' for the call.
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [Parameter(Mandatory=$true)][string[]]$Arguments
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>$null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{
        Output   = ($out | Out-String).Trim()
        ExitCode = $code
    }
}

function Assert-InsideDept([string]$Path, [string]$What) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $dept = [System.IO.Path]::GetFullPath($DeptRoot)
    if (-not $full.StartsWith($dept, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ("REFUSE: {0} target escapes departments/workbuddy: {1}" -f $What, $full)
    }
    return $full
}

function Log-Launcher([string]$Message) {
    # Durable progress log: survives console-capture problems and gives the
    # orchestrator/STATUS evidence of which decision the launcher reached.
    $ts = (Get-Date).ToUniversalTime().ToString('o')
    try {
        $logDir = Join-Path $DeptRoot 'logs'
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        Add-Content -Path (Join-Path $logDir 'launcher-progress.log') -Value ("[{0}] {1}" -f $ts, $Message) -Encoding UTF8
    } catch { }
}

function Test-PortFree([int]$Port) {
    # Free = nothing from adb AND nothing listening on loopback for this port.
    $devList = (Invoke-Native -Exe $AdbExe -Arguments @('devices')).Output
    if ($devList -match ("emulator-{0}\b" -f $Port)) { return $false }
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Select-FreePort {
    $candidates = 5554, 5556, 5558, 5560, 5562, 5564, 5566, 5568, 5570, 5572
    foreach ($p in $candidates) {
        if (Test-PortFree $p) { return $p }
    }
    throw "ABORT: no free even port in candidates $($candidates -join ', '). Report only; no foreign instance was killed."
}

function Get-AvdNameFromConfig {
    $cfg = Join-Path $AvdHome ($AvdName + '.avd\config.ini')
    if (-not (Test-Path $cfg)) { return $null }
    # the emulator rewrites config.ini with spaces around '=' on first boot,
    # so the pattern must tolerate optional whitespace
    $m = Select-String -Path $cfg -Pattern '^AvdId\s*=\s*(.+?)\s*$'
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim() }
    return $null
}

function Get-EmuAvdName([string]$Ser) {
    # `adb -s <serial> emu avd name` replies with the AVD name followed by the
    # console protocol's "OK" terminator line(s); compare the first non-empty line.
    $out = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $Ser, 'emu', 'avd', 'name')).Output
    if (-not $out) { return '' }
    $first = ($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if ($first) { return $first.Trim() }
    return ''
}

function Write-State([string]$Path, [hashtable]$State) {
    Assert-InsideDept $Path 'state file' | Out-Null
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    ($State | ConvertTo-Json -Depth 4) | Set-Content -Path $Path -Encoding ASCII
}

function Read-State([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try {
        return Get-Content -Path $Path -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Stop-OurInstance {
    # Stop ONLY an instance whose ownership can be confirmed:
    #  - state file gives serial/pid, AND
    #  - either the recorded PID is a live emulator process whose command line
    #    mentions our AVD name, or `emu avd name` on the serial reports our AVD.
    # Uncertain results are reported as failure (exit 1), never exit 0.
    param([string]$StatePath)

    $state = Read-State $StatePath
    if ($null -eq $state) {
        Log-Launcher "stop: no state file; nothing to stop"
        Write-Host "No instance state file at $StatePath; nothing recorded to stop."
        return $true
    }
    $serial = $state.serial
    $port = [int]$state.port
    $avd = $state.avd

    $devList = (Invoke-Native -Exe $AdbExe -Arguments @('devices')).Output
    $present = ($devList -match ("emulator-{0}\b" -f $port))
    if (-not $present) {
        Write-Host ("Instance {0} not present in adb devices; nothing to stop." -f $serial)
        return $true
    }

    # ownership check via the emulator itself
    $avdReported = Get-EmuAvdName $serial
    if ($avdReported -ne $avd) {
        Log-Launcher ("stop REFUSED: emu avd name reports '{0}', expected '{1}'" -f $avdReported, $avd)
        Write-Host ("REFUSING to stop {0}: `emu avd name` reports '{1}', expected '{2}'. Foreign instance left alone." -f $serial, $avdReported, $avd)
        return $false
    }

    Write-Host ("Stopping our instance {0} via emu kill (shared adb server untouched)..." -f $serial)
    Invoke-Native -Exe $AdbExe -Arguments @('-s', $serial, 'emu', 'kill') | Out-Null

    $deadline = (Get-Date).AddSeconds(90)
    $gone = $false
    while ((Get-Date) -lt $deadline) {
        $now = (Invoke-Native -Exe $AdbExe -Arguments @('devices')).Output
        if ($now -notmatch ("emulator-{0}\b" -f $port)) { $gone = $true; break }
        Start-Sleep -Seconds 3
    }
    if (-not $gone) {
        Write-Host ("FAILURE: {0} still listed by adb 90s after emu kill. Shutdown result uncertain." -f $serial)
        return $false
    }
    # process check when we know the pid
    if ($state.pid) {
        $proc = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -eq 'emulator') {
            Write-Host ("FAILURE: emulator PID {0} still alive after serial disappeared. Shutdown result uncertain." -f $state.pid)
            return $false
        }
    }
    Write-Host ("Instance {0} stopped and verified gone." -f $serial)
    return $true
}

# ------------------------- stop mode -------------------------
if ($Stop) {
    Log-Launcher ("stop mode requested (state={0})" -f $StateFile)
    $ok = Stop-OurInstance -StatePath $StateFile
    Log-Launcher ("stop mode result ok={0}" -f $ok)
    if (-not $ok) { exit 1 }
    exit 0
}

# ------------------------- boot preflight -------------------------
foreach ($p in @($EmulatorExe, $AdbExe)) {
    if (-not (Test-Path $p)) { throw ("MISSING: {0}. Next step: check the SDK path; do not install anything automatically." -f $p) }
}
$avdIni = Join-Path $AvdHome ($AvdName + '.ini')
$avdCfg = Join-Path $AvdHome ($AvdName + '.avd\config.ini')
if (-not (Test-Path $avdIni) -or -not (Test-Path $avdCfg)) {
    throw ("AVD not prepared: {0} or {1} missing. Next step: run prepare-baseline.ps1 first." -f $avdIni, $avdCfg)
}
$cfgAvdId = Get-AvdNameFromConfig
if ($cfgAvdId -ne $AvdName) {
    Log-Launcher ("REFUSE avd id mismatch: config='{0}' expected='{1}'" -f $cfgAvdId, $AvdName)
    throw ("REFUSE: config.ini AvdId is '{0}', expected '{1}'. Existing AVD data is never erased or repurposed." -f $cfgAvdId, $AvdName)
}
Log-Launcher ("boot mode: preflight ok, avd={0}" -f $cfgAvdId)

# process-scoped AVD home only
$env:ANDROID_AVD_HOME = $AvdHome
Write-Host ("ANDROID_AVD_HOME (this process only) = {0}" -f $AvdHome)

# acceleration gate: never silently fall back to unaccelerated
$accel = Invoke-Native -Exe $EmulatorExe -Arguments @('-accel-check')
if ($accel.ExitCode -ne 0) {
    throw ("ABORT: hardware acceleration is not usable (exit {0}). Refusing to start unaccelerated.`nManual fix (max 3 steps): 1) enable 'Windows Hypervisor Platform' in Windows features, 2) reboot if prompted, 3) re-run `emulator -accel-check` until it exits 0. See https://developer.android.com/studio/run/emulator-acceleration" -f $accel.ExitCode)
}
Write-Host ("accel-check OK (exit 0): {0}" -f ($accel.Output -replace "`r?`n", ' '))
Log-Launcher "accel-check ok"

# no foreign instance may be disturbed: if ANY android device is attached, stop.
$preDev = (Invoke-Native -Exe $AdbExe -Arguments @('devices')).Output
$foreign = ($preDev -split "`n" | Where-Object { $_ -match '^\S+\s+(device|offline|unauthorized)' })
if ($foreign) {
    Log-Launcher ("REFUSE foreign device attached: {0}" -f ($foreign -join ';'))
    throw ("ABORT: android device(s) already attached - refusing to start another instance. Attached:`n{0}" -f ($foreign -join "`n"))
}
Log-Launcher "no foreign devices attached"

# ------------------------- port selection -------------------------
if ($Port -ne 0) {
    if ($Port % 2 -ne 0 -or $Port -lt 5554 -or $Port -gt 5684) {
        throw ("ABORT: -Port must be an even port in 5554..5684, got {0}." -f $Port)
    }
    if (-not (Test-PortFree $Port)) {
        throw ("ABORT: requested port {0} is not free. Pick another even port; foreign instances are never killed." -f $Port)
    }
} else {
    $Port = Select-FreePort
}
$Serial = ("emulator-{0}" -f $Port)
Write-Host ("Selected port {0} (serial {1})" -f $Port, $Serial)
Log-Launcher ("selected port {0} serial {1}" -f $Port, $Serial)

# ------------------------- env sanitize (Start-Process guard) -------------------
# Some shells inject BOTH HTTPS_PROXY and https_proxy into the process environment
# block. Start-Process rebuilds the child environment in a case-sensitive
# dictionary and throws on such duplicates (verified on this host). Drop the
# lowercase variants in THIS process scope only; the emulator does not use host
# proxy settings (guest traffic is NATed).
foreach ($dup in @('http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy', 'no_proxy')) {
    try { [Environment]::SetEnvironmentVariable($dup, $null, 'Process') } catch { }
}

# ------------------------- launch (hidden) -------------------------
Assert-InsideDept $LogRoot 'log dir' | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runLogDir = Join-Path $LogRoot ("start-$stamp-port$Port")
New-Item -ItemType Directory -Path $runLogDir -Force | Out-Null
$stdoutLog = Join-Path $runLogDir 'emulator-stdout.log'
$stderrLog = Join-Path $runLogDir 'emulator-stderr.log'

$emuArgs = @(
    '-avd', $AvdName,
    '-port', ("{0}" -f $Port),
    '-no-window',
    '-no-audio',
    '-no-snapshot',
    '-camera-back', 'none',
    '-camera-front', 'none',
    '-memory', '1536',
    '-cores', '2',
    '-gpu', 'software',
    '-accel', 'on'
)
Write-Host ("Launching: {0} {1}" -f $EmulatorExe, ($emuArgs -join ' '))

$proc = $null
$bootOk = $false
$avdVerified = $false
try {
    $proc = Start-Process -FilePath $EmulatorExe -ArgumentList $emuArgs `
                           -WindowStyle Hidden `
                           -RedirectStandardOutput $stdoutLog `
                           -RedirectStandardError  $stderrLog `
                           -PassThru
    Write-Host ("Emulator process started, PID={0}" -f $proc.Id)
    Log-Launcher ("emulator launched pid={0} port={1}" -f $proc.Id, $Port)

    # record instance identity immediately
    Write-State $StateFile @{
        pid = $proc.Id
        serial = $Serial
        port = $Port
        avd = $AvdName
        logdir = $runLogDir
        started_utc = (Get-Date).ToUniversalTime().ToString('o')
    }

    # ------------------------- wait for boot -------------------------
    $deadline = (Get-Date).AddSeconds($BootTimeoutSec)
    $lastErr = ''
    $pollCount = 0
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) {
            $ec = $proc.ExitCode
            Log-Launcher ("emulator process exited early code={0} after {1} polls" -f $ec, $pollCount)
            Write-Host "----- emulator stdout -----"; if (Test-Path $stdoutLog) { Get-Content $stdoutLog -Tail 60 }
            Write-Host "----- emulator stderr -----"; if (Test-Path $stderrLog) { Get-Content $stderrLog -Tail 60 }
            throw ("ABORT: emulator exited early with code {0}. Logs kept in {1}" -f $ec, $runLogDir)
        }
        $prop = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $Serial, 'shell', 'getprop', 'sys.boot_completed')).Output
        $pollCount++
        Log-Launcher ("poll {0}: boot_completed='{1}'" -f $pollCount, $prop)
        if ($prop -eq '1') { $bootOk = $true; break }
        $lastErr = $prop
        Start-Sleep -Seconds $PollIntervalSec
    }
    Log-Launcher ("wait loop ended bootOk={0} polls={1}" -f $bootOk, $pollCount)

    if (-not $bootOk) {
        Write-Host "----- emulator stdout -----"; if (Test-Path $stdoutLog) { Get-Content $stdoutLog -Tail 60 }
        Write-Host "----- emulator stderr -----"; if (Test-Path $stderrLog) { Get-Content $stderrLog -Tail 60 }
        throw ("ABORT: sys.boot_completed did not reach 1 within {0}s (last getprop output: '{1}'). Logs kept in {2}" -f $BootTimeoutSec, $lastErr, $runLogDir)
    }

    # ------------------------- ownership verification -------------------------
    $avdReported = Get-EmuAvdName $Serial
    if ($avdReported -ne $AvdName) {
        Log-Launcher ("ownership check failed: emu avd name reports '{0}'" -f $avdReported)
        throw ("ABORT: ownership check failed: `emu avd name` on {0} reports '{1}', expected '{2}'. Refusing to use a foreign instance." -f $Serial, $avdReported, $AvdName)
    }
    $avdVerified = $true
    Log-Launcher ("boot ok serial={0} pid={1} ownership verified" -f $Serial, $proc.Id)

    # ------------------------- record evidence -------------------------
    $bootLine = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $Serial, 'shell', 'getprop', 'ro.build.version.release')).Output
    $abiLine  = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $Serial, 'shell', 'getprop', 'ro.product.cpu.abilist')).Output
    $devicesNow = (Invoke-Native -Exe $AdbExe -Arguments @('devices')).Output

    Write-Host ""
    Write-Host "BOOT OK"
    Write-Host ("  PID                 : {0}" -f $proc.Id)
    Write-Host ("  serial / port       : {0} (port {1}, explicitly selected)" -f $Serial, $Port)
    Write-Host ("  AVD ownership       : verified via `emu avd name` = {0}" -f $avdReported)
    Write-Host ("  sys.boot_completed  : 1 (verified)")
    Write-Host ("  Android release     : {0}  abilist: {1}" -f $bootLine, $abiLine)
    Write-Host ("  logs                : {0}" -f $runLogDir)
    Write-Host ("  adb devices         :`n{0}" -f $devicesNow)
    Write-Host ("  normal shutdown     : .\start-baseline.ps1 -Stop -StateFile {0}" -f $StateFile)
}
catch {
    Log-Launcher ("EXCEPTION in boot path: {0}: {1} at {2}" -f $_.Exception.GetType().Name, $_.Exception.Message, $_.InvocationInfo.Line)
    Write-Host ("EXCEPTION: {0}" -f $_.Exception.Message)
    throw
}
finally {
    # finally cleanup: if anything above failed AFTER we started the process,
    # stop exactly the instance we started (ownership already recorded).
    if ($proc -and -not $avdVerified) {
        Log-Launcher "finally: boot not verified; stopping own instance"
        Write-Host "finally: boot did not complete successfully; stopping the instance this script started..."
        Stop-OurInstance -StatePath $StateFile | Out-Null
    }
}
# ------------------------- optional clean shutdown -------------------------
if ($AutoStop) {
    Write-Host ""
    Write-Host ("AutoStop: shutting down {0} ..." -f $Serial)
    $ok = Stop-OurInstance -StatePath $StateFile
    if (-not $ok) {
        Write-Host "AutoStop FAILED: shutdown result uncertain (reported as failure, not exit 0)."
        exit 1
    }
}

exit 0
