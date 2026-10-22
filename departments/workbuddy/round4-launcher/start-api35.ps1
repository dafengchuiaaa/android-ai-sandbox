#Requires -Version 5.1
<#
.SYNOPSIS
  Boot, inspect or stop the project-dedicated hidden Android 15 emulator
  instance (AVD ai-sandbox-api35-google, profile api35-google-x86_64).

.DESCRIPTION
  Round4 task 35 candidate (dispatch 35 / overview 33). Companion of
  prepare-api35.ps1; the old API33 baseline launcher, its AVD data and its
  state files are never read, written, repurposed or stopped by this script.

  Modes:
    (default)  boot: preflight -> device gate -> accel gate -> port -> launch
                        -> bounded boot wait -> ownership + API/ABI verify
    -Stop      stop exactly the instance recorded in -StateFile, with
               re-verified serial / on-site AVD name / process identity
    -Status    read-only status report of the recorded instance

  State file (overview 33 fields, baseline-compatible):
    profile_id, avd, avd_home, image_dir, api_level, abi,
    pid, pid_created_utc, pid_proc_name, pid_module, argv,
    serial, port, logdir, started_utc, verified_* , boot_completed
  Default location: <Round4Root>\instances\instance-<id>.json

  HARDENING (vs old start-baseline.ps1):
    * Explicit port selection and occupancy check; no fixed port.
    * Refuses to start when ANY android device is attached (unknown device or
      an already-running instance of this profile). Foreign instances are
      only reported, never killed.
    * Ownership is re-verified before shutdown: serial present in adb devices,
      `emu avd name` reports our AVD, and the recorded root PID still has the
      same creation time / process name / module path (PID reuse detection).
    * Never calls `adb kill-server`, never kills by process name, never
      force-kills a stale PID. Shutdown goes through `adb emu kill` on the
      verified serial only; an uncertain result is a FAILURE (exit 1) and the
      state file is preserved for recovery.
    * Path containment uses GetFullPath + separator boundary + reparse-point
      detection (the old bare StartsWith prefix check is gone).
    * try/finally cleans up only an instance THIS script started and could not
      fully verify.

  FORBIDDEN by dispatch (this script does none of them):
    running the real emulator from the offline tests, adb kill-server,
    batch kill by process name, force kill by stale PID. The requested RAM
    (1536 MB) and display (720x1280) stay as the baseline; LowRam is opt-in.

  TEST SEAMS (offline verification only; they relocate the sandbox but never
  relax any safety rule):
    -SdkRoot / -EmulatorExe / -AdbExe / -Round4Root

.PARAMETER Port
  Even TCP port 5554..5684. 0 (default) = auto-select the first free one.

.PARAMETER BootTimeoutSec
  Max seconds to wait for sys.boot_completed=1 (default 300).

.PARAMETER PollIntervalSec
  Seconds between boot polls (default 5).

.PARAMETER StopTimeoutSec
  Max seconds to wait for the serial to disappear / the process to exit
  during shutdown verification (default 90).

.PARAMETER StateFile
  Instance state JSON. Boot: default is derived under
  <Round4Root>\instances. Stop/Status: REQUIRED (never guessed).

.PARAMETER Stop
  Stop mode.

.PARAMETER Status
  Read-only status mode.

.PARAMETER AutoStop
  Boot mode: after full verification, shut the instance down immediately.

.PARAMETER LowRam
  Explicit boot-only opt-in: append the emulator -lowram flag so the existing
  1536 MB request can bypass the emulator's automatic RAM floor. Default is off.
  This does not imply Android Go mode or guarantee host memory/CPU usage.

.EXAMPLE
  .\start-api35.ps1 -Port 5556
  .\start-api35.ps1 -Port 5556 -AutoStop
  .\start-api35.ps1 -Status  -StateFile .\runtime\round4\instances\instance-....json
  .\start-api35.ps1 -Stop    -StateFile .\runtime\round4\instances\instance-....json
#>

[CmdletBinding(DefaultParameterSetName = 'Boot')]
param(
    [Parameter(ParameterSetName = 'Boot')]
    [int]$Port = 0,

    [Parameter(ParameterSetName = 'Boot')]
    [int]$BootTimeoutSec = 300,

    [Parameter(ParameterSetName = 'Boot')]
    [int]$PollIntervalSec = 5,

    [Parameter(ParameterSetName = 'Boot')]
    [switch]$AutoStop,

    [Parameter(ParameterSetName = 'Boot')]
    [switch]$LowRam,

    [Parameter(ParameterSetName = 'Stop')]
    [switch]$Stop,

    [Parameter(ParameterSetName = 'Status')]
    [switch]$Status,

    [int]$StopTimeoutSec = 90,

    [string]$StateFile = '',

    [string]$SdkRoot = 'C:\Users\LOCAL_USER\AppData\Local\Android\Sdk',
    [string]$Round4Root = '',
    [string]$EmulatorExe = '',
    [string]$AdbExe = '',
    [switch]$TestMode,
    [ValidateRange(1, 30)][int]$NativeTimeoutSec = 15
)

$ErrorActionPreference = 'Stop'

# ------------------------- frozen identity -------------------------
$ProfileId = 'api35-google-x86_64'
$AvdName   = 'ai-sandbox-api35-google'
$ApiLevel  = 35
$Abi       = 'x86_64'
$TagId     = 'google_apis'
$ImageRel  = 'system-images\android-35\google_apis\x86_64'
$PortCandidates = 5554, 5556, 5558, 5560, 5562, 5564, 5566, 5568, 5570, 5572

# ------------------------- path helpers -------------------------
function Get-NormalizedPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-ReparsePointUnder([string]$RootFull, [string]$TargetFull) {
    # Include the root itself and all ancestors, not only its descendants.
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
    # what stops "<root>-evil" prefix siblings and "<root>\link -> elsewhere".
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

# ------------------------- native command wrapper -------------------------
function Invoke-Native {
    # Windows PowerShell 5.1 raises a TERMINATING NativeCommandError whenever a
    # native command writes to stderr while $ErrorActionPreference='Stop'.
    # Drop to 'Continue' for the call (verified on this host).
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [Parameter(Mandatory=$true)][string[]]$Arguments,
        [switch]$AllowFailure
    )
    # Start one child without a shell; drain both pipes concurrently and bound
    # both process execution and pipe EOF (ADB may spawn a shared server).
    $quoted = foreach ($arg in $Arguments) {
        if ($arg.Length -gt 0 -and $arg -notmatch '[\s"]') { $arg }
        else {
            $escaped = [regex]::Replace($arg, '(\\*)"', '$1$1\"')
            $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
            '"' + $escaped + '"'
        }
    }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Exe
    $info.Arguments = $quoted -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $child = New-Object System.Diagnostics.Process
    $child.StartInfo = $info
    try {
        if (-not $child.Start()) { throw 'Native child could not start' }
        $outTask = $child.StandardOutput.ReadToEndAsync()
        $errTask = $child.StandardError.ReadToEndAsync()
        if (-not $child.WaitForExit($NativeTimeoutSec * 1000)) {
            # Kill only the exact command process created above; never the
            # shared server, device process or an externally supplied PID.
            try { $child.Kill(); $child.WaitForExit(2000) | Out-Null } catch { }
            Log-Launcher ("native TIMEOUT: {0} {1}" -f $Exe, ($Arguments -join ' '))
            throw ("driver_timeout: native command exceeded {0}s: {1}" -f $NativeTimeoutSec, $Exe)
        }
        if (-not $outTask.Wait(3000) -or -not $errTask.Wait(3000)) {
            throw 'driver_timeout: native output pipes did not close within 3s'
        }
        $code = $child.ExitCode
        $out = $outTask.Result.Trim()
        $err = $errTask.Result.Trim()
        Log-Launcher ("native exit={0} argv={1} stderr={2}" -f $code, ($Arguments -join ' '), $err)
        if ($code -ne 0 -and -not $AllowFailure) {
            throw ("native command failed exit={0}: {1} {2}; {3}" -f $code, $Exe, ($Arguments -join ' '), $err)
        }
        return [pscustomobject]@{ Output=$out; ExitCode=$code; Error=$err }
    } finally {
        $child.Dispose()
    }
}

# ------------------------- logging -------------------------
$script:LogFile = ''
function Log-Launcher([string]$Message) {
    $ts = (Get-Date).ToUniversalTime().ToString('o')
    try {
        if (-not $script:LogFile) { return }
        $dir = Split-Path -Parent $script:LogFile
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -LiteralPath $script:LogFile -Value ("[{0}] {1}" -f $ts, $Message) -Encoding UTF8
    } catch { }
}

# ------------------------- state helpers -------------------------
function Write-State([string]$Path, [hashtable]$State) {
    Assert-InsideRoot $Path $Round4Root 'state file' | Out-Null
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    ($State | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $Path -Encoding ASCII
}

function Read-State([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        if ($record -isnot [System.Management.Automation.PSCustomObject]) { throw 'state must be an object' }
        return $record
    } catch {
        throw ("REFUSE: invalid instance state JSON at {0}: {1}" -f $Path, $_.Exception.Message)
    }
}

function Assert-StateIdentity($State) {
    foreach ($field in @('pid','pid_created_utc','pid_proc_name','pid_module','serial','port','avd','profile_id','api_level','abi','avd_home','image_dir')) {
        if ($null -eq $State.$field -or [string]::IsNullOrWhiteSpace([string]$State.$field)) { throw ("REFUSE: state missing required identity field {0}" -f $field) }
    }
    if ([int]$State.pid -le 0) { throw 'REFUSE: state pid must be positive' }
    $created = [datetime]::Parse([string]$State.pid_created_utc).ToUniversalTime()
    if ($created -gt (Get-Date).ToUniversalTime().AddSeconds(1)) { throw 'REFUSE: state creation time is in the future' }
    $portNumber = [int]$State.port
    if ($portNumber -lt 5554 -or $portNumber -gt 5684 -or $portNumber % 2 -ne 0 -or [string]$State.serial -cne "emulator-$portNumber") { throw 'REFUSE: state serial/port mismatch or invalid port' }
    if ($State.profile_id -cne $ProfileId -or $State.avd -cne $AvdName -or [int]$State.api_level -ne $ApiLevel -or $State.abi -cne $Abi) { throw 'REFUSE: state does not match the fixed API35 profile' }
    if ((Get-NormalizedPath $State.avd_home) -ine $AvdHome -or (Get-NormalizedPath $State.image_dir) -ine $ImageDir -or (Get-NormalizedPath $State.pid_module) -ine $EmulatorExe) { throw 'REFUSE: state paths do not match the fixed instance/executable' }
}

function Get-ProcessIdentity([int]$ProcId) {
    # Identity of a live process WITHOUT WMI/CIM: name + creation time +
    # module path. Returns $null when the PID is not alive.
    try {
        $p = Get-Process -Id $ProcId -ErrorAction Stop
    } catch {
        return $null
    }
    $id = [ordered]@{
        pid          = $ProcId
        name         = $p.ProcessName
        start_utc    = $null
        start_error  = $null
        module       = $null
        module_error = $null
    }
    try { $id.start_utc = $p.StartTime.ToUniversalTime().ToString('o') } catch { $id.start_error = $_.Exception.Message }
    try { $id.module = $p.MainModule.FileName } catch { $id.module_error = $_.Exception.Message }
    return $id
}

function Test-IdentityMatches($State, $Identity) {
    # Returns @{ ok = bool; reason = string }
    if ($null -eq $Identity) {
        return @{ ok = $false; reason = 'recorded PID is no longer alive while serial is online; ownership uncertain' }
    }
    if (-not $State.pid_created_utc -or -not $State.pid_proc_name -or -not $State.pid_module) { return @{ ok=$false; reason='missing process identity fields' } }
    if ($Identity.start_error) {
        return @{ ok = $false; reason = ("cannot read process start time (pid {0}): {1}" -f $Identity.pid, $Identity.start_error) }
    }
    if ($State.pid_created_utc) {
        try {
            $a = [datetime]::Parse($Identity.start_utc).ToUniversalTime()
            $b = [datetime]::Parse([string]$State.pid_created_utc).ToUniversalTime()
            if ([math]::Abs(($a - $b).TotalMilliseconds) -gt 1) {
                return @{ ok = $false; reason = ("PID reuse or identity change: recorded creation {0}, live process created {1}" -f $b.ToString('o'), $a.ToString('o')) }
            }
        } catch {
            return @{ ok = $false; reason = ("cannot compare creation times: {0}" -f $_.Exception.Message) }
        }
    }
    if ($State.pid_proc_name -and ($Identity.name -cne [string]$State.pid_proc_name)) {
        return @{ ok = $false; reason = ("process name changed: recorded '{0}', live '{1}'" -f $State.pid_proc_name, $Identity.name) }
    }
    if ($State.pid_module) {
        if ($Identity.module) {
            if ($Identity.module -cne [string]$State.pid_module) {
                return @{ ok = $false; reason = ("module path changed: recorded '{0}', live '{1}'" -f $State.pid_module, $Identity.module) }
            }
        } elseif ($Identity.module_error) {
            return @{ ok = $false; reason = ("cannot read module path of pid {0}: {1}" -f $Identity.pid, $Identity.module_error) }
        }
    }
    return @{ ok = $true; reason = 'pid identity confirmed (creation time/name/module)' }
}

# ------------------------- adb helpers -------------------------
function Get-AdbDeviceLines {
    $out = (Invoke-Native -Exe $AdbExe -Arguments @('devices')).Output
    $lines = @($out -split "`r?`n" | Where-Object { $_ -match '^\S+\s+(device|offline|unauthorized)' } | ForEach-Object { $_.Trim() })
    # -NoEnumerate is REQUIRED: at a function boundary PowerShell unwraps a
    # single-element array to a scalar string, and piping a string enumerates
    # its characters (device checks would silently see zero devices).
    return Write-Output -NoEnumerate $lines
}

function Get-EmuAvdName([string]$Ser) {
    # `adb -s <serial> emu avd name` replies with the AVD name followed by the
    # console protocol's "OK" terminator; compare the first non-empty line.
    $out = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $Ser, 'emu', 'avd', 'name')).Output
    if (-not $out) { return '' }
    $first = ($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if ($first) { return $first.Trim() }
    return ''
}

function Test-PortFree([int]$Port) {
    # Free = nothing in `adb devices` AND nothing listening on loopback.
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
    foreach ($p in $PortCandidates) {
        if (Test-PortFree $p) { return $p }
    }
    throw ("ABORT: no free even port in candidates {0}. Report only; no foreign instance was killed." -f ($PortCandidates -join ', '))
}

# ------------------------- config inspection -------------------------
function Get-AvdConfigValue([string]$ConfigPath, [string]$Key) {
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return $null }
    $m = Select-String -LiteralPath $ConfigPath -Pattern ("^{0}\s*=\s*(.*?)\s*$" -f [regex]::Escape($Key))
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim() }
    return $null
}

# ------------------------- shutdown (ownership-gated) -------------------------
function Stop-OurInstance {
    # Stop ONLY an instance whose ownership can be confirmed at stop time:
    #   * state file belongs to THIS profile (profile_id match), AND
    #   * serial is present in `adb devices`, AND
    #   * `emu avd name` on that serial reports our AVD, AND
    #   * the recorded root PID still matches (creation time / name / module).
    # Uncertain results are FAILURE (exit 1), never exit 0. No adb kill-server,
    # no process-name batch kill, no force kill by stale PID.
    param([string]$StatePath)

    $state = Read-State $StatePath
    if ($null -eq $state) {
        Log-Launcher 'stop: no (readable) state file; nothing to stop'
        Write-Host ("No readable instance state at {0}; nothing recorded to stop." -f $StatePath)
        return $true
    }
    if ([string]$state.profile_id -cne $ProfileId) {
        Log-Launcher ("stop REFUSED: state profile_id='{0}' is not '{1}'" -f $state.profile_id, $ProfileId)
        Write-Host ("REFUSING to stop: state file {0} belongs to profile '{1}', not '{2}'. Old baseline states must be stopped with the old launcher." -f $StatePath, $state.profile_id, $ProfileId)
        return $false
    }
    Assert-StateIdentity $state
    $serial = [string]$state.serial
    $port   = [int]$state.port
    $avd    = [string]$state.avd

    $devLines = Get-AdbDeviceLines
    $present = ($devLines | Where-Object { $_ -match ("^emulator-{0}\s" -f $port) })
    if (-not $present) {
        # Serial gone: only accept if our recorded process is also gone.
        if ($state.pid) {
            $idCheck = Get-ProcessIdentity ([int]$state.pid)
            if ($null -ne $idCheck) {
                Write-Host ("FAILURE: serial {0} is gone but recorded PID {1} is still alive. Not killing by PID; shutdown result uncertain." -f $serial, $state.pid)
                Log-Launcher ("stop UNCERTAIN: serial gone but pid {0} alive" -f $state.pid)
                return $false
            }
        }
        Write-Host ("Instance {0} not present in adb devices; nothing to stop." -f $serial)
        return $true
    }

    # on-site AVD name must be ours
    $avdReported = Get-EmuAvdName $serial
    if ($avdReported -cne $avd -or $avdReported -cne $AvdName) {
        Log-Launcher ("stop REFUSED: emu avd name reports '{0}', expected '{1}'" -f $avdReported, $avd)
        Write-Host ("REFUSING to stop {0}: 'emu avd name' reports '{1}', expected '{2}'. Foreign instance left alone." -f $serial, $avdReported, $avd)
        return $false
    }

    # process identity (PID reuse / identity change) before any action
    if ($state.pid) {
        $identity = Get-ProcessIdentity ([int]$state.pid)
        $match = Test-IdentityMatches $state $identity
        if (-not $match.ok) {
            Log-Launcher ("stop REFUSED: {0}" -f $match.reason)
            Write-Host ("REFUSING to stop {0}: {1}. Foreign or reused PID left alone." -f $serial, $match.reason)
            return $false
        }
        Log-Launcher ("stop identity ok: {0}" -f $match.reason)
    } else {
        Log-Launcher 'stop: state has no pid; relying on serial + avd name ownership'
    }

    Write-Host ("Stopping our instance {0} via emu kill (shared adb server untouched)..." -f $serial)
    Invoke-Native -Exe $AdbExe -Arguments @('-s', $serial, 'emu', 'kill') | Out-Null

    $deadline = (Get-Date).AddSeconds($StopTimeoutSec)
    $gone = $false
    while ((Get-Date) -lt $deadline) {
        $devLines = Get-AdbDeviceLines
        if (-not ($devLines | Where-Object { $_ -match ("^emulator-{0}\s" -f $port) })) { $gone = $true; break }
        Start-Sleep -Seconds 3
    }
    if (-not $gone) {
        Write-Host ("FAILURE: {0} still listed by adb {1}s after emu kill. Shutdown result uncertain; state file preserved, no force kill." -f $serial, $StopTimeoutSec)
        Log-Launcher ("stop UNCERTAIN: serial {0} still listed after {1}s" -f $serial, $StopTimeoutSec)
        return $false
    }

    # serial gone: bounded wait for the recorded root process to exit
    if ($state.pid) {
        $pDeadline = (Get-Date).AddSeconds([math]::Min(30, $StopTimeoutSec))
        $exited = $false
        while ((Get-Date) -lt $pDeadline) {
            if ($null -eq (Get-ProcessIdentity ([int]$state.pid))) { $exited = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $exited) {
            Write-Host ("FAILURE: serial {0} gone but emulator PID {1} is still alive after {2}s. Not force-killing; shutdown result uncertain." -f $serial, $state.pid, [math]::Min(30, $StopTimeoutSec))
            Log-Launcher ("stop UNCERTAIN: serial gone but pid {0} still alive" -f $state.pid)
            return $false
        }
    }
    Write-Host ("Instance {0} stopped and verified gone (serial + process)." -f $serial)
    Log-Launcher ("stop OK serial={0}" -f $serial)
    return $true
}

# ------------------------- resolve targets -------------------------
if ([string]::IsNullOrWhiteSpace($SdkRoot)) { throw 'ABORT: -SdkRoot must not be empty.' }
$SdkRoot = Get-NormalizedPath $SdkRoot
if ([string]::IsNullOrWhiteSpace($EmulatorExe)) { $EmulatorExe = Join-Path $SdkRoot 'emulator\emulator.exe' }
if ([string]::IsNullOrWhiteSpace($AdbExe)) { $AdbExe = Join-Path $SdkRoot 'platform-tools\adb.exe' }
$EmulatorExe = Get-NormalizedPath $EmulatorExe
$AdbExe      = Get-NormalizedPath $AdbExe

if ([string]::IsNullOrWhiteSpace($Round4Root)) {
    # script lives in departments/workbuddy/round4-launcher -> up ONE level
    # (to departments/workbuddy) then runtime\round4. Overview 33 freezes the
    # AVD/state/logs root at <project>/departments/workbuddy/runtime/round4.
    $Round4Root = Join-Path $PSScriptRoot ('..\runtime\round4')
}
$Round4Root = Get-NormalizedPath $Round4Root

# Production paths are fixed. Offline doubles require an explicit mode and
# must remain below the integration test root, never an arbitrary caller root.
$ProjectRoot = Get-NormalizedPath (Join-Path $PSScriptRoot '..\..\..')
$FixedSdk = 'C:\Users\LOCAL_USER\AppData\Local\Android\Sdk'
$FixedRuntime = Join-Path $ProjectRoot 'departments\workbuddy\runtime\round4'
if ($TestMode) {
    $testRoot = Join-Path $ProjectRoot 'integration\round4\tests'
    foreach ($testPath in @($SdkRoot,$Round4Root,$EmulatorExe,$AdbExe)) { Assert-InsideRoot $testPath $testRoot 'offline test path' | Out-Null }
} elseif ($SdkRoot -ine $FixedSdk -or $Round4Root -ine $FixedRuntime -or $EmulatorExe -ine (Join-Path $FixedSdk 'emulator\emulator.exe') -or $AdbExe -ine (Join-Path $FixedSdk 'platform-tools\adb.exe')) {
    throw 'REFUSE: production SDK, executables and runtime paths are fixed; offline overrides require -TestMode'
}
Assert-InsideRoot $Round4Root $Round4Root 'runtime root' | Out-Null
foreach ($dup in @('http_proxy','https_proxy','all_proxy','ftp_proxy','no_proxy')) { [Environment]::SetEnvironmentVariable($dup,$null,'Process') }

$ImageDir     = Join-Path $SdkRoot $ImageRel
$AvdHome      = Join-Path $Round4Root 'avd'
$InstancesDir = Join-Path $Round4Root 'instances'
$LogRoot      = Join-Path $Round4Root 'logs'
$script:LogFile = Join-Path $LogRoot 'launcher-progress.log'
$AvdIni       = Join-Path $AvdHome ($AvdName + '.ini')
$AvdConfig    = Join-Path $AvdHome ($AvdName + '.avd\config.ini')

if ($BootTimeoutSec -lt 1)  { throw 'ABORT: -BootTimeoutSec must be >= 1.' }
if ($PollIntervalSec -lt 1) { throw 'ABORT: -PollIntervalSec must be >= 1.' }
if ($StopTimeoutSec -lt 1)  { throw 'ABORT: -StopTimeoutSec must be >= 1.' }

# ============================ STOP MODE ============================
if ($Stop) {
    if ([string]::IsNullOrWhiteSpace($StateFile)) {
        Write-Host 'ABORT: -Stop requires an explicit -StateFile (instance state is never guessed).'
        exit 1
    }
    $StateFile = Assert-InsideRoot $StateFile $Round4Root 'state file'
    Log-Launcher ("stop mode requested (state={0})" -f $StateFile)
    $ok = Stop-OurInstance -StatePath $StateFile
    Log-Launcher ("stop mode result ok={0}" -f $ok)
    if (-not $ok) { exit 1 }
    exit 0
}

# ============================ STATUS MODE ============================
if ($Status) {
    if ([string]::IsNullOrWhiteSpace($StateFile)) {
        Write-Host 'ABORT: -Status requires an explicit -StateFile.'
        exit 1
    }
    $StateFile = Assert-InsideRoot $StateFile $Round4Root 'state file'
    $state = Read-State $StateFile
    if ($null -eq $state) {
        Write-Host ("STATUS: no readable state at {0}; nothing recorded." -f $StateFile)
        exit 1
    }
    if ([string]$state.profile_id -cne $ProfileId) {
        Write-Host ("STATUS: state at {0} belongs to profile '{1}', not '{2}'." -f $StateFile, $state.profile_id, $ProfileId)
        exit 1
    }
    Assert-StateIdentity $state
    $serial = [string]$state.serial
    $port   = [int]$state.port
    $devLines = Get-AdbDeviceLines
    $present = @($devLines | Where-Object { $_ -match ("^emulator-{0}\s" -f $port) })
    $avdReported = ''
    $sdk = ''; $release = ''; $abilist = ''
    if ($present.Count -gt 0) {
        $avdReported = Get-EmuAvdName $serial
        $sdk     = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $serial, 'shell', 'getprop', 'ro.build.version.sdk')).Output
        $release = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $serial, 'shell', 'getprop', 'ro.build.version.release')).Output
        $abilist = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $serial, 'shell', 'getprop', 'ro.product.cpu.abilist')).Output
    }
    $identity = $null
    if ($state.pid) { $identity = Get-ProcessIdentity ([int]$state.pid) }
    $idMatch = Test-IdentityMatches $state $identity

    Write-Host 'INSTANCE STATUS'
    Write-Host ("  state file      : {0}" -f $StateFile)
    Write-Host ("  profile_id      : {0}" -f $state.profile_id)
    Write-Host ("  avd             : {0}" -f $state.avd)
    Write-Host ("  serial / port   : {0} (port {1})" -f $serial, $port)
    Write-Host ("  pid             : {0} (created {1})" -f $state.pid, $state.pid_created_utc)
    Write-Host ("  pid identity    : {0} - {1}" -f $(if ($idMatch.ok) { 'OK' } else { 'MISMATCH' }), $idMatch.reason)
    Write-Host ("  adb devices     : {0}" -f $(if ($present.Count -gt 0) { ($present -join ';') } else { 'serial not listed' }))
    Write-Host ("  emu avd name    : {0}" -f $(if ($avdReported) { $avdReported } else { 'n/a' }))
    Write-Host ("  ro.build.version.sdk : {0}" -f $(if ($sdk) { $sdk } else { 'n/a' }))
    Write-Host ("  release / abilist: {0} / {1}" -f $(if ($release) { $release } else { 'n/a' }), $(if ($abilist) { $abilist } else { 'n/a' }))

    $ok = $true
    if ($present.Count -eq 0)      { $ok = $false }
    if ($avdReported -cne $AvdName){ $ok = $false }
    if ($sdk -ne [string]$ApiLevel){ $ok = $false }
    if ($abilist -notmatch [regex]::Escape($Abi)) { $ok = $false }
    if (-not $idMatch.ok)          { $ok = $false }
    Write-Host ("STATUS RESULT: {0}" -f $(if ($ok) { 'consistent with profile api35-google-x86_64' } else { 'INCONSISTENT - investigate before any action' }))
    if (-not $ok) { exit 1 }
    exit 0
}

# ============================ BOOT MODE ============================
# ------------------------- preflight: executables -------------------------
foreach ($p in @($EmulatorExe, $AdbExe)) {
    if (-not (Test-Path -LiteralPath $p)) {
        throw ("MISSING: {0}. Next step: check the SDK path; do not install anything automatically." -f $p)
    }
}

# ------------------------- preflight: AVD content -------------------------
if (-not (Test-Path -LiteralPath $AvdIni) -or -not (Test-Path -LiteralPath $AvdConfig)) {
    throw ("AVD not prepared: {0} or {1} missing. Next step: run prepare-api35.ps1 first." -f $AvdIni, $AvdConfig)
}
$cfgAvdId = Get-AvdConfigValue $AvdConfig 'AvdId'
if ($cfgAvdId -cne $AvdName) {
    Log-Launcher ("REFUSE avd id mismatch: config='{0}' expected='{1}'" -f $cfgAvdId, $AvdName)
    throw ("REFUSE: config.ini AvdId is '{0}', expected '{1}'. Existing AVD data is never erased or repurposed." -f $cfgAvdId, $AvdName)
}
$cfgTarget = Get-AvdConfigValue $AvdConfig 'target'
if ($cfgTarget -cne ("android-{0}" -f $ApiLevel)) {
    throw ("REFUSE: config.ini target is '{0}', expected 'android-{1}'. Wrong AVD content." -f $cfgTarget, $ApiLevel)
}
$cfgSysdir = Get-AvdConfigValue $AvdConfig 'image.sysdir.1'
if ($cfgSysdir) {
    $want = $ImageDir.TrimEnd('\') + '\'
    if ($cfgSysdir.TrimEnd('\') -ine $ImageDir.TrimEnd('\')) {
        throw ("REFUSE: config.ini image.sysdir.1 is '{0}', expected '{1}'. Wrong system image binding." -f $cfgSysdir, $want)
    }
}
$cfgTag = Get-AvdConfigValue $AvdConfig 'tag.id'
if ($cfgTag -and $cfgTag -cne $TagId) {
    throw ("REFUSE: config.ini tag.id is '{0}', expected '{1}'. Wrong image tag." -f $cfgTag, $TagId)
}
Log-Launcher ("boot mode: preflight ok, avd={0}, target={1}" -f $cfgAvdId, $cfgTarget)

# ------------------------- device gate (before touching the emulator) -------------------------
$preDev = Get-AdbDeviceLines
if ($preDev.Count -gt 0) {
    $ownRunning = $false
    foreach ($line in $preDev) {
        $ser = ($line -split '\s+')[0]
        if ($ser -match '^emulator-\d+$') {
            $reported = Get-EmuAvdName $ser
            if ($reported -ceq $AvdName) { $ownRunning = $true }
        }
    }
    if ($ownRunning) {
        Log-Launcher ("REFUSE: an instance of {0} is already running: {1}" -f $AvdName, ($preDev -join ';'))
        throw ("ABORT: an instance of AVD '{0}' is already running. Stop it first (start-api35.ps1 -Stop -StateFile <its state file>); this script never takes over or kills a running instance. Attached:`n{1}" -f $AvdName, ($preDev -join "`n"))
    }
    Log-Launcher ("REFUSE foreign device attached: {0}" -f ($preDev -join ';'))
    throw ("ABORT: android device(s) already attached - refusing to start another instance. Attached:`n{0}" -f ($preDev -join "`n"))
}
Log-Launcher 'device gate: no android device attached'

# ------------------------- accel gate -------------------------
$env:ANDROID_AVD_HOME = $AvdHome
Write-Host ("ANDROID_AVD_HOME (this process only) = {0}" -f $AvdHome)

$accel = Invoke-Native -Exe $EmulatorExe -Arguments @('-accel-check')
if ($accel.ExitCode -ne 0) {
    throw ("ABORT: hardware acceleration is not usable (exit {0}). Refusing to start unaccelerated.`nManual fix (max 3 steps): 1) enable 'Windows Hypervisor Platform' in Windows features, 2) reboot if prompted, 3) re-run 'emulator -accel-check' until it exits 0. See https://developer.android.com/studio/run/emulator-acceleration" -f $accel.ExitCode)
}
Write-Host ("accel-check OK (exit 0): {0}" -f ($accel.Output -replace "`r?`n", ' '))
Log-Launcher 'accel-check ok'

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

# ------------------------- state file target -------------------------
if ([string]::IsNullOrWhiteSpace($StateFile)) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $StateFile = Join-Path $InstancesDir ("instance-{0}-p{1}.json" -f $stamp, $Port)
}
$StateFile = Assert-InsideRoot $StateFile $Round4Root 'state file'

# ------------------------- env sanitize (Start-Process guard) -------------------
# Some shells inject BOTH HTTPS_PROXY and https_proxy; Start-Process rebuilds
# the child environment case-sensitively and throws on duplicates. Drop the
# lowercase variants in THIS process scope only (guest traffic is NATed).
foreach ($dup in @('http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy', 'no_proxy')) {
    try { [Environment]::SetEnvironmentVariable($dup, $null, 'Process') } catch { }
}

# ------------------------- launch (hidden) -------------------------
$runLogDir = Assert-InsideRoot (Join-Path $LogRoot ("start-{0}-port{1}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $Port)) $Round4Root 'log dir'
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
if ($LowRam) { $emuArgs += '-lowram' }
Write-Host ("Launching: {0} {1}" -f $EmulatorExe, ($emuArgs -join ' '))

$proc = $null
$fullyVerified = $false
try {
    $proc = Start-Process -FilePath $EmulatorExe -ArgumentList $emuArgs `
                           -WindowStyle Hidden `
                           -RedirectStandardOutput $stdoutLog `
                           -RedirectStandardError  $stderrLog `
                           -PassThru
    Write-Host ("Emulator process started, PID={0}" -f $proc.Id)
    Log-Launcher ("emulator launched pid={0} port={1}" -f $proc.Id, $Port)

    # record instance identity immediately (overview 33 fields)
    $identity = Get-ProcessIdentity $proc.Id
    Write-State $StateFile @{
        profile_id      = $ProfileId
        avd             = $AvdName
        avd_home        = $AvdHome
        image_dir       = $ImageDir
        api_level       = $ApiLevel
        abi             = $Abi
        pid             = $proc.Id
        pid_created_utc = $(if ($identity -and -not $identity.start_error) { $identity.start_utc } else { $null })
        pid_proc_name   = $(if ($identity) { $identity.name } else { $null })
        pid_module      = $(if ($identity -and -not $identity.module_error) { $identity.module } else { $null })
        argv            = @($EmulatorExe) + $emuArgs
        serial          = $Serial
        port            = $Port
        logdir          = $runLogDir
        started_utc     = (Get-Date).ToUniversalTime().ToString('o')
        boot_completed  = $false
    }

    # ------------------------- wait for boot -------------------------
    $deadline = (Get-Date).AddSeconds($BootTimeoutSec)
    $lastErr = ''
    $pollCount = 0
    $bootOk = $false
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) {
            # Defensive exit-code read: on this host, Windows PowerShell 5.1
            # Start-Process -PassThru WITH stream redirection does not expose
            # the child exit code (verified with real .exe targets too). The
            # early-exit detection itself (HasExited) is unaffected; the code
            # is reported as 'unknown' when it cannot be retrieved and the
            # captured logs remain the evidence.
            $ec = $null
            try { $ec = $proc.ExitCode } catch { $ec = $null }
            $ecText = $(if ($null -ne $ec -and "$ec" -ne '') { "$ec" } else { 'unknown' })
            Log-Launcher ("emulator process exited early code={0} after {1} polls" -f $ecText, $pollCount)
            Write-Host '----- emulator stdout -----'; if (Test-Path -LiteralPath $stdoutLog) { Get-Content -LiteralPath $stdoutLog -Tail 60 }
            Write-Host '----- emulator stderr -----'; if (Test-Path -LiteralPath $stderrLog) { Get-Content -LiteralPath $stderrLog -Tail 60 }
            throw ("ABORT: emulator exited early with code {0}. Logs kept in {1}" -f $ecText, $runLogDir)
        }
        $propResult = Invoke-Native -Exe $AdbExe -Arguments @('-s', $Serial, 'shell', 'getprop', 'sys.boot_completed') -AllowFailure
        $prop = $(if ($propResult.ExitCode -eq 0) { $propResult.Output } else { '' })
        $pollCount++
        Log-Launcher ("poll {0}: boot_completed='{1}'" -f $pollCount, $prop)
        if ($prop -eq '1') { $bootOk = $true; break }
        $lastErr = $prop
        Start-Sleep -Seconds $PollIntervalSec
    }
    Log-Launcher ("wait loop ended bootOk={0} polls={1}" -f $bootOk, $pollCount)

    if (-not $bootOk) {
        Write-Host '----- emulator stdout -----'; if (Test-Path -LiteralPath $stdoutLog) { Get-Content -LiteralPath $stdoutLog -Tail 60 }
        Write-Host '----- emulator stderr -----'; if (Test-Path -LiteralPath $stderrLog) { Get-Content -LiteralPath $stderrLog -Tail 60 }
        throw ("ABORT: sys.boot_completed did not reach 1 within {0}s (last getprop output: '{1}'). Logs kept in {2}" -f $BootTimeoutSec, $lastErr, $runLogDir)
    }

    # ------------------------- ownership verification -------------------------
    $avdReported = Get-EmuAvdName $Serial
    if ($avdReported -cne $AvdName) {
        Log-Launcher ("ownership check failed: emu avd name reports '{0}'" -f $avdReported)
        throw ("ABORT: ownership check failed: 'emu avd name' on {0} reports '{1}', expected '{2}'. Refusing to use a foreign instance." -f $Serial, $avdReported, $AvdName)
    }

    # ------------------------- on-site API/ABI verification -------------------------
    $sdkLine  = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $Serial, 'shell', 'getprop', 'ro.build.version.sdk')).Output
    $abiLine  = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $Serial, 'shell', 'getprop', 'ro.product.cpu.abilist')).Output
    $relLine  = (Invoke-Native -Exe $AdbExe -Arguments @('-s', $Serial, 'shell', 'getprop', 'ro.build.version.release')).Output
    if ($sdkLine -cne [string]$ApiLevel) {
        Log-Launcher ("on-site API mismatch: sdk='{0}' expected '{1}'" -f $sdkLine, $ApiLevel)
        throw ("ABORT: on-site ro.build.version.sdk is '{0}', expected {1} (profile {2}). The running device does not match this profile." -f $sdkLine, $ApiLevel, $ProfileId)
    }
    if ($abiLine -notmatch [regex]::Escape($Abi)) {
        Log-Launcher ("on-site ABI mismatch: abilist='{0}' expected to contain '{1}'" -f $abiLine, $Abi)
        throw ("ABORT: on-site ro.product.cpu.abilist is '{0}', expected to contain '{1}' (profile {2})." -f $abiLine, $Abi, $ProfileId)
    }

    $liveIdentity = Get-ProcessIdentity $proc.Id
    $identityMatch = Test-IdentityMatches ([pscustomobject]@{pid_created_utc=$identity.start_utc; pid_proc_name=$identity.name; pid_module=$identity.module}) $liveIdentity
    if (-not $identityMatch.ok) { throw ("REFUSE: startup process identity changed: {0}" -f $identityMatch.reason) }
    $fullyVerified = $true

    # ------------------------- record evidence -------------------------
    $devicesNow = (Invoke-Native -Exe $AdbExe -Arguments @('devices')).Output
    Write-State $StateFile @{
        profile_id      = $ProfileId
        avd             = $AvdName
        avd_home        = $AvdHome
        image_dir       = $ImageDir
        api_level       = $ApiLevel
        abi             = $Abi
        pid             = $proc.Id
        pid_created_utc = $(if ($identity -and -not $identity.start_error) { $identity.start_utc } else { $null })
        pid_proc_name   = $(if ($identity) { $identity.name } else { $null })
        pid_module      = $(if ($identity -and -not $identity.module_error) { $identity.module } else { $null })
        argv            = @($EmulatorExe) + $emuArgs
        serial          = $Serial
        port            = $Port
        logdir          = $runLogDir
        started_utc     = (Get-Date).ToUniversalTime().ToString('o')
        boot_completed  = $true
        verified_avd    = $avdReported
        verified_sdk    = $sdkLine
        verified_release= $relLine
        verified_abilist= $abiLine
        verified_utc    = (Get-Date).ToUniversalTime().ToString('o')
    }

    Write-Host ''
    Write-Host 'BOOT OK'
    Write-Host ("  profile_id        : {0}" -f $ProfileId)
    Write-Host ("  PID               : {0} (created {1})" -f $proc.Id, $(if ($identity -and -not $identity.start_error) { $identity.start_utc } else { 'unknown' }))
    Write-Host ("  serial / port     : {0} (port {1}, explicitly selected)" -f $Serial, $Port)
    Write-Host ("  AVD ownership     : verified via 'emu avd name' = {0}" -f $avdReported)
    Write-Host ("  sys.boot_completed: 1 (verified)")
    Write-Host ("  Android sdk/release: {0} / {1}  abilist: {2}" -f $sdkLine, $relLine, $abiLine)
    Write-Host ("  avd_home          : {0}" -f $AvdHome)
    Write-Host ("  image_dir         : {0}" -f $ImageDir)
    Write-Host ("  argv              : {0} {1}" -f $EmulatorExe, ($emuArgs -join ' '))
    Write-Host ("  state file        : {0}" -f $StateFile)
    Write-Host ("  logs              : {0}" -f $runLogDir)
    Write-Host ("  adb devices       :`n{0}" -f $devicesNow)
    Write-Host ("  normal shutdown   : .\start-api35.ps1 -Stop -StateFile {0}" -f $StateFile)
}
catch {
    Log-Launcher ("EXCEPTION in boot path: {0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message)
    Write-Host ("EXCEPTION: {0}" -f $_.Exception.Message)
    throw
}
finally {
    # finally cleanup: stop exactly the instance THIS script started when it
    # could not be fully verified (Stop-OurInstance re-checks ownership).
    if ($proc -and -not $fullyVerified) {
        Log-Launcher 'finally: instance not fully verified; stopping own instance'
        Write-Host 'finally: instance not fully verified; stopping the instance this script started...'
        Stop-OurInstance -StatePath $StateFile | Out-Null
    }
}

# ------------------------- optional clean shutdown -------------------------
if ($AutoStop) {
    Write-Host ''
    Write-Host ("AutoStop: shutting down {0} ..." -f $Serial)
    $ok = Stop-OurInstance -StatePath $StateFile
    if (-not $ok) {
        Write-Host 'AutoStop FAILED: shutdown result uncertain (reported as failure, not exit 0).'
        exit 1
    }
}

exit 0
